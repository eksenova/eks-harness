from __future__ import annotations

import json
import logging
import os
import secrets
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any

from eks_harness import __version__
from eks_harness.auth.exposure import ExposureError, ensure_safe_bind
from eks_harness.config import Config, env_name
from eks_harness.config import load as load_config
from eks_harness.daemon import fds
from eks_harness.paths import Paths, resolve_paths
from eks_harness.pools.backends import WINDOWS, alive, pid_started

log = logging.getLogger("eks_harness.server")

CONTROL_HEADER = "X-Harness-Control"
GRACEFUL_SHUTDOWN_SECONDS = 8
LOG_FORMAT = "[daemon %(asctime)s] %(message)s"


class ServeError(RuntimeError):
    def __init__(self, message: str, code: int = 1) -> None:
        super().__init__(message)
        self.code = code


@dataclass
class ServeOptions:
    managed: bool = False
    host: str | None = None
    port: int | None = None
    force: bool = False
    fake_pools: bool | None = None
    argv: list[str] = field(default_factory=list)


def read_daemon_info(paths: Paths) -> dict | None:
    try:
        data = json.loads(paths.daemon_info_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def running_daemon(paths: Paths) -> dict | None:
    info = read_daemon_info(paths)
    if not info or not alive(info.get("pid"), info.get("started") or None):
        return None
    return info


def write_daemon_info(paths: Paths, info: dict) -> None:
    target = paths.daemon_info_file
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(info, indent=2) + "\n")
    if not WINDOWS:
        os.chmod(tmp, 0o600)
    os.replace(tmp, target)


def remove_daemon_info(paths: Paths) -> None:
    info = read_daemon_info(paths)
    if info and info.get("pid") == os.getpid():
        paths.daemon_info_file.unlink(missing_ok=True)


def control_request(info: dict, action: str, timeout: float = 5.0) -> dict:
    url = str(info.get("url") or "").rstrip("/")
    if not url:
        raise ServeError("the daemon info file has no URL")
    request = urllib.request.Request(f"{url}/api/daemon/{action}", data=b"{}", method="POST",
                                     headers={"Content-Type": "application/json",
                                              CONTROL_HEADER: str(info.get("controlToken") or ""),
                                              "User-Agent": f"eks-harness/{__version__}"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", "replace")
        raise ServeError(f"the daemon refused to {action}: HTTP {error.code} {body[:300]}") from error
    except OSError as error:
        raise ServeError(f"the daemon did not answer at {url}: {error}") from error


class SingletonLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.handle: IO[str] | None = None

    def try_acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+")
        try:
            if WINDOWS:
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False
        self.handle = handle
        return True

    def acquire(self, timeout: float) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.try_acquire():
                return True
            time.sleep(0.25)
        return False

    def release(self) -> None:
        if self.handle is not None:
            try:
                self.handle.close()
            finally:
                self.handle = None


def configure_logging() -> None:
    root = logging.getLogger()
    for existing in [h for h in root.handlers if not getattr(h, "_eks_harness", False)]:
        root.removeHandler(existing)
    if not any(getattr(h, "_eks_harness", False) for h in root.handlers):
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter(LOG_FORMAT, "%Y-%m-%dT%H:%M:%S%z"))
        handler._eks_harness = True
        root.addHandler(handler)
    root.setLevel(logging.INFO)
    for noisy in ("uvicorn.access", "httpx", "httpcore", "watchfiles"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def restart_command(options: ServeOptions) -> list[str]:
    return [sys.executable, "-m", "eks_harness.cli", "daemon", "serve", *options.argv]


def apply_overrides(options: ServeOptions) -> None:
    if options.host:
        os.environ[env_name("server.host")] = options.host
    if options.port is not None:
        os.environ[env_name("server.port")] = str(options.port)
    if options.fake_pools is not None:
        os.environ["EKS_HARNESS_FAKE_POOLS"] = "1" if options.fake_pools else "0"


def check_bind(config: Config, force: bool) -> None:
    try:
        ensure_safe_bind(config, force)
    except ExposureError as error:
        raise ServeError(str(error), 2) from error


def take_over(paths: Paths, lock: SingletonLock, managed: bool) -> None:
    if lock.try_acquire():
        return
    info = running_daemon(paths)
    if not managed:
        where = info.get("url") if info else "unknown URL"
        raise ServeError(f"another eks-harness daemon is already running ({where}); exiting", 0)
    if info:
        log.info("a daemon is already running at %s; asking it to stop so the service takes over", info.get("url"))
        try:
            control_request(info, "stop")
        except ServeError as error:
            log.info("%s", error)
    if not lock.acquire(120):
        raise ServeError(f"the running daemon did not release {paths.lock_file} within 120s")


def bound_port(server: Any, fallback: int) -> int:
    for listener in getattr(server, "servers", []) or []:
        for sock in getattr(listener, "sockets", []) or []:
            try:
                return int(sock.getsockname()[1])
            except (OSError, IndexError, TypeError):
                continue
    return fallback


def serve(options: ServeOptions | None = None, paths: Paths | None = None) -> int:
    import uvicorn

    from eks_harness.daemon.app import create_app

    options = options or ServeOptions()
    configure_logging()
    fds.raise_file_limit()
    apply_overrides(options)
    paths = (paths or resolve_paths()).ensure()
    config = load_config(paths)
    check_bind(config, options.force)
    lock = SingletonLock(paths.lock_file)
    take_over(paths, lock, options.managed)
    state = {"restart": False}
    try:
        app = create_app(config, paths, fake_pools=options.fake_pools, managed=options.managed)
        ctx = app.state.ctx
        token = secrets.token_urlsafe(32)
        ctx.control_token = token
        uv_config = uvicorn.Config(app, host=config.listen_host(), port=config.listen_port(), log_level="warning",
                                   access_log=False, lifespan="on", proxy_headers=False,
                                   timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_SECONDS)
        server = uvicorn.Server(uv_config)

        def request_restart() -> None:
            state["restart"] = True
            server.should_exit = True

        def request_stop() -> None:
            server.should_exit = True

        ctx.restart_handler = request_restart
        ctx.stop_handler = request_stop

        def announce() -> None:
            while not server.started and not server.should_exit:
                time.sleep(0.05)
            if not server.started:
                return
            port = bound_port(server, config.listen_port())
            host = config.listen_host()
            local_host = "127.0.0.1" if host in ("0.0.0.0", "", "localhost") else host
            url = f"http://{'[' + local_host + ']' if ':' in local_host else local_host}:{port}"
            ctx.url = url
            ctx.pools.host.url = url
            write_daemon_info(paths, {
                "pid": os.getpid(), "started": pid_started(os.getpid()), "url": url, "host": host, "port": port,
                "publicUrl": config.public_url(), "managed": options.managed, "fakePools": ctx.pools.fake,
                "controlToken": token, "version": __version__, "sourceHash": ctx.version.get("sourceHash"),
                "startedAt": time.time(), "logFile": str(paths.daemon_log),
            })
            log.info("listening on %s (%s; config %s)%s", url, "service" if options.managed else "on demand",
                     paths.config_file, " with fake pools" if ctx.pools.fake else "")

        threading.Thread(target=announce, name="announce", daemon=True).start()
        server.run()
    finally:
        remove_daemon_info(paths)
        lock.release()
    if state["restart"]:
        command = restart_command(options)
        log.info("restarting: %s", " ".join(command))
        sys.stdout.flush()
        sys.stderr.flush()
        os.execv(command[0], command)
    log.info("stopped")
    return 0


def serve_argv(managed: bool, host: str | None, port: int | None, force: bool,
               fake_pools: bool | None) -> list[str]:
    argv: list[str] = []
    if managed:
        argv.append("--managed")
    if host:
        argv += ["--host", host]
    if port is not None:
        argv += ["--port", str(port)]
    if force:
        argv.append("--force")
    if fake_pools:
        argv.append("--fake-pools")
    return argv


def wait_until_up(paths: Paths, timeout: float, previous_pid: int | None = None) -> dict | None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        info = running_daemon(paths)
        if info and info.get("pid") != previous_pid and health_ok(info):
            return info
        time.sleep(0.2)
    return None


def wait_until_down(paths: Paths, pid: int, started: str | None, timeout: float) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not alive(pid, started):
            return True
        time.sleep(0.2)
    return not alive(pid, started)


def health_ok(info: dict, timeout: float = 2.0) -> bool:
    url = str(info.get("url") or "").rstrip("/")
    if not url:
        return False
    try:
        with urllib.request.urlopen(f"{url}/api/health", timeout=timeout) as response:
            return response.status == 200
    except OSError:
        return False


def spawn_daemon(paths: Paths, extra: Sequence[str] = ()) -> int:
    import subprocess

    from eks_harness.pools.backends import detached_kwargs

    paths.log_dir.mkdir(parents=True, exist_ok=True)
    with open(paths.daemon_log, "ab", buffering=0) as handle:
        process = subprocess.Popen([sys.executable, "-m", "eks_harness.cli", "daemon", "serve", *extra],
                                   stdin=subprocess.DEVNULL, stdout=handle, stderr=subprocess.STDOUT, close_fds=True,
                                   **detached_kwargs())
    return process.pid
