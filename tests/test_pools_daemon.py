from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from eks_harness.daemon import server
from eks_harness.paths import resolve_paths


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def cli(*args: str, env: dict, timeout: float = 90) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "eks_harness.cli", *args], env=env, capture_output=True, text=True,
                          timeout=timeout)


@pytest.fixture
def daemon(harness_home: Path):
    port = free_port()
    env = dict(os.environ)
    env["EKS_HARNESS_SERVER_PORT"] = str(port)
    env["EKS_HARNESS_FAKE_POOLS"] = "1"
    env.pop("EKS_HARNESS_URL", None)
    paths = resolve_paths(env).ensure()
    log = open(paths.daemon_log, "ab")
    process = subprocess.Popen([sys.executable, "-m", "eks_harness.cli", "daemon", "serve", "--fake-pools"], env=env,
                               stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    info = server.wait_until_up(paths, 60)
    try:
        assert info is not None, paths.daemon_log.read_text(errors="replace")
        yield env, paths, info, process
    finally:
        if process.poll() is None:
            process.send_signal(signal.SIGTERM)
            try:
                process.wait(20)
            except subprocess.TimeoutExpired:
                process.kill()
        current = server.read_daemon_info(paths)
        if current and current.get("pid") not in (None, process.pid) and server.running_daemon(paths):
            os.kill(int(current["pid"]), signal.SIGTERM)
        log.close()


def test_daemon_serves_restarts_gracefully_and_stops(daemon) -> None:
    env, paths, info, process = daemon
    assert info["fakePools"] is True and info["url"].endswith(env["EKS_HARNESS_SERVER_PORT"])
    assert (paths.daemon_info_file.stat().st_mode & 0o777) == 0o600
    status = cli("daemon", "status", "--json", env=env)
    assert status.returncode == 0, status.stderr
    data = json.loads(status.stdout)
    assert data["running"] is True and data["url"] == info["url"] and data["fakePools"] is True

    acquired = cli("lease", "acquire", "--kind", "ios", "--project", "acme/mobile-app", "--session", "main",
                   "--instance", "test:daemon", "--json", env=env)
    assert acquired.returncode == 0, acquired.stderr
    lease = json.loads(acquired.stdout)
    sid = lease["sid"]
    assert lease["status"] == "granted"
    shell = cli("lease", "acquire", "--kind", "ios", "--project", "acme/mobile-app", "--session", "main",
                "--instance", "test:daemon", "--shell", env=env)
    assert f"EKS_SID={sid}" in shell.stdout and "EKS_DEVICE_UDID=FAKE-IOS-0001" in shell.stdout

    shot = cli("capture", "screenshot", "--sid", sid, "--caption", "home", env=env)
    assert shot.returncode == 0, shot.stderr
    links = shot.stdout.strip().splitlines()
    assert len(links) == 3 and "/raw/" in links[0] and "/a/" in links[1] and links[2].endswith("/s/main")

    restarted = cli("daemon", "restart", env=env)
    assert restarted.returncode == 0, restarted.stderr + restarted.stdout
    after = server.running_daemon(paths)
    assert after is not None and after["pid"] == process.pid and after["startedAt"] != info["startedAt"]
    assert after["controlToken"] != info["controlToken"]
    shown = cli("lease", "show", sid, "--json", env=env)
    assert shown.returncode == 0, shown.stderr
    assert json.loads(shown.stdout)["state"] == "active"
    beat = cli("lease", "heartbeat", sid, "--json", env=env)
    assert beat.returncode == 0 and json.loads(beat.stdout)["state"] == "active"

    released = cli("lease", "release", sid, env=env)
    assert released.returncode == 0
    gone = cli("capture", "screenshot", "--sid", sid, env=env)
    assert gone.returncode == 7 and "lease resume" in gone.stderr
    resumed = cli("lease", "resume", sid, "--json", env=env)
    assert resumed.returncode == 0, resumed.stderr
    assert json.loads(resumed.stdout)["lease"]["previousSid"] == sid

    devices = cli("devices", "list", "--json", env=env)
    assert devices.returncode == 0 and len(json.loads(devices.stdout)["items"]) == 6
    logs = cli("logs", "daemon", "-n", "50", env=env)
    assert logs.returncode == 0 and "listening on" in logs.stdout

    stopped = cli("daemon", "stop", env=env)
    assert stopped.returncode == 0, stopped.stderr
    process.wait(30)
    assert process.returncode == 0
    assert server.running_daemon(paths) is None
    down = cli("daemon", "status", "--json", env=env)
    assert down.returncode == 3 and json.loads(down.stdout)["running"] is False


def test_second_daemon_leaves_quietly(daemon) -> None:
    env, paths, info, process = daemon
    second = cli("daemon", "serve", "--fake-pools", env=env, timeout=60)
    assert second.returncode == 0
    assert server.running_daemon(paths)["pid"] == process.pid


def test_non_loopback_bind_without_auth_is_refused(harness_home: Path) -> None:
    env = dict(os.environ)
    env["EKS_HARNESS_SERVER_HOST"] = "0.0.0.0"
    env["EKS_HARNESS_SERVER_PORT"] = str(free_port())
    refused = cli("daemon", "serve", "--fake-pools", env=env, timeout=60)
    assert refused.returncode == 2 and "authentication disabled" in refused.stderr


def test_restart_command_reexecutes_the_package() -> None:
    options = server.ServeOptions(managed=True, argv=server.serve_argv(True, None, 7200, False, True))
    assert server.restart_command(options) == [sys.executable, "-m", "eks_harness.cli", "daemon", "serve",
                                               "--managed", "--port", "7200", "--fake-pools"]
