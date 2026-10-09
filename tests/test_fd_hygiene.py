from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from eks_harness.db import open_database


def open_fds() -> int:
    for folder in ("/dev/fd", "/proc/self/fd"):
        if os.path.isdir(folder):
            return len(os.listdir(folder))
    pytest.skip("no fd listing on this platform")


def fd_path(fd: int) -> str | None:
    try:
        if os.path.isdir("/proc/self/fd"):
            return os.readlink(f"/proc/self/fd/{fd}")
        import fcntl

        raw = fcntl.fcntl(fd, getattr(fcntl, "F_GETPATH"), bytes(1024))
        return raw.split(b"\0", 1)[0].decode(errors="replace")
    except (OSError, AttributeError):
        return None


def files_open(path: Path) -> int:
    folder = "/proc/self/fd" if os.path.isdir("/proc/self/fd") else "/dev/fd"
    if not os.path.isdir(folder):
        pytest.skip("no fd listing on this platform")
    name = path.resolve().name
    count = 0
    for entry in os.listdir(folder):
        target = fd_path(int(entry)) if entry.isdigit() else None
        if target and Path(target).name.startswith(name):
            count += 1
    return count


def touch_from_threads(db, rounds: int) -> None:
    for _ in range(rounds):
        worker = threading.Thread(target=lambda: db.execute("SELECT 1").fetchone())
        worker.start()
        worker.join()


def test_connections_of_finished_threads_are_closed(tmp_path: Path) -> None:
    db = open_database(tmp_path / "harness.db")
    try:
        touch_from_threads(db, 5)
        baseline = open_fds()
        touch_from_threads(db, 200)
        assert db.open_connections() <= 2
        assert open_fds() <= baseline + 4
    finally:
        db.close()


def test_the_calling_threads_connection_survives_reaping(tmp_path: Path) -> None:
    db = open_database(tmp_path / "harness.db")
    try:
        mine = db.conn()
        touch_from_threads(db, 20)
        assert db.conn() is mine
        assert mine.execute("SELECT 1").fetchone()[0] == 1
    finally:
        db.close()


def test_many_requests_keep_database_handles_flat(client: TestClient, paths) -> None:
    def cycle() -> None:
        acquired = client.post("/api/leases/acquire", params={"waitSeconds": 5},
                               json={"kind": "ios", "project": "acme/mobile-app", "session": "main",
                                     "instance": "fd-check"})
        assert acquired.status_code == 200, acquired.text
        assert client.post(f"/api/leases/{acquired.json()['sid']}/release").status_code == 200
        assert client.get("/api/status").status_code == 200

    for _ in range(5):
        cycle()
    before_fds, before_db = open_fds(), files_open(paths.db_file)
    assert before_db >= 1
    for _ in range(100):
        cycle()
    assert files_open(paths.db_file) <= before_db + 6
    assert open_fds() <= before_fds + 12


def test_a_stream_abandoned_mid_file_closes_the_file(tmp_path: Path) -> None:
    import anyio

    from eks_harness.store import serving

    video = tmp_path / "clip.mp4"
    video.write_bytes(b"x" * (serving.CHUNK * 4))

    async def abandon() -> None:
        stream = serving.closing_stream(serving._iter_file(video, 0, video.stat().st_size))
        assert await stream.__anext__()
        assert files_open(video) == 1
        await stream.aclose()

    for _ in range(50):
        anyio.run(abandon)
    assert files_open(video) == 0


def test_a_cancelled_stream_closes_the_file(tmp_path: Path) -> None:
    import anyio

    from eks_harness.store import serving

    video = tmp_path / "clip.mp4"
    video.write_bytes(b"x" * (serving.CHUNK * 4))

    async def disconnect() -> None:
        async def consume() -> None:
            async for _ in serving.closing_stream(serving._iter_file(video, 0, video.stat().st_size)):
                await anyio.sleep(10)

        with anyio.move_on_after(0.2):
            await consume()

    anyio.run(disconnect)
    assert files_open(video) == 0


def test_housekeeping_closes_connections_of_finished_threads(ctx) -> None:
    from eks_harness.daemon.housekeeping import housekeeping

    touch_from_threads(ctx.db, 30)
    report = housekeeping(ctx).run_once()
    assert report.get("closedConnections", 0) >= 1
    assert ctx.db.open_connections() <= 3


def test_status_reports_file_descriptor_usage(client: TestClient) -> None:
    descriptors = client.get("/api/status").json()["fileDescriptors"]
    assert descriptors["open"] > 0
    assert descriptors["dbConnections"] >= 1
    assert descriptors["pressure"] is False


def test_file_usage_flags_pressure_near_the_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    from eks_harness.daemon import fds

    monkeypatch.setattr(fds, "open_fds", lambda: 230)
    monkeypatch.setattr(fds, "file_limits", lambda: (256, 1024))
    assert fds.usage(120) == {"open": 230, "limit": 256, "dbConnections": 120, "pressure": True}


def test_daemon_start_raises_a_low_file_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    import resource

    from eks_harness.daemon import fds

    calls = []
    monkeypatch.setattr(resource, "getrlimit", lambda kind: (256, resource.RLIM_INFINITY))
    monkeypatch.setattr(resource, "setrlimit", lambda kind, value: calls.append(value))
    assert fds.raise_file_limit() == (fds.FILE_LIMIT, resource.RLIM_INFINITY)
    assert calls == [(fds.FILE_LIMIT, resource.RLIM_INFINITY)]
    calls.clear()
    monkeypatch.setattr(resource, "getrlimit", lambda kind: (256, 1024))
    assert fds.raise_file_limit() == (1024, 1024)
    monkeypatch.setattr(resource, "getrlimit", lambda kind: (65536, resource.RLIM_INFINITY))
    assert fds.raise_file_limit() == (65536, resource.RLIM_INFINITY)
    assert calls == [(1024, 1024)]


def test_service_definitions_raise_the_file_limit(paths, tmp_path: Path) -> None:
    from eks_harness import service

    spec = service.build_spec(paths, command=[str(tmp_path / "eks-harness"), "daemon", "serve", "--managed"])
    plist = service.launchd_plist(spec)
    limit = f"<key>NumberOfFiles</key><integer>{service.FILE_LIMIT}</integer>"
    assert f"<key>SoftResourceLimits</key><dict>{limit}</dict>" in plist
    assert f"<key>HardResourceLimits</key><dict>{limit}</dict>" in plist
    assert f"LimitNOFILE={service.FILE_LIMIT}\n" in service.systemd_unit(spec)
