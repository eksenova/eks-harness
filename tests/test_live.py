from __future__ import annotations

import base64
import contextlib
import json
import random
import re
import shutil
import socket
import struct
import subprocess
import sys
import textwrap
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.testclient import TestClient
from websockets.sync.server import serve

from eks_harness.api import routes_live
from eks_harness.config import Config
from eks_harness.db.repos import leases as leases_repo
from eks_harness.live import LiveHub, LiveUnavailable, StreamSettings, profile_target
from eks_harness.live import ios as ios_module
from eks_harness.live.android import AndroidLiveSource
from eks_harness.live.chrome import ChromeScreencastSource
from eks_harness.live.common import LiveError
from eks_harness.live.h264 import AnnexBCutter, AvcConfig, MovStreamDemuxer, StreamSyncError, find_avcc, parse_avcc
from eks_harness.live.ios import IosLiveSource
from eks_harness.live.jpeg import JpegSplitter, is_jpeg
from eks_harness.live.process import MjpegTranscoder
from eks_harness.live.sources import profile_contexts
from eks_harness.pools.base import LiveSource
from eks_harness.pools.fake_media import TINY_JPEG

from conftest import AuthEnv, add_grant, bearer, create_key, create_user, ensure_session, make_app

FFMPEG = shutil.which("ffmpeg")
needs_ffmpeg = pytest.mark.skipif(FFMPEG is None, reason="ffmpeg is not installed")
SIMCTL_AVCC = bytes.fromhex("01640032ffe1001127640032ac565013005279aa6a021a020401000428ee3cb0fdf8f800")


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@contextlib.contextmanager
def running(app: FastAPI) -> Iterator[str]:
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="on"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started:
        assert thread.is_alive() and time.monotonic() < deadline, "test server did not start"
        time.sleep(0.02)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(30)


class PartReader:
    def __init__(self, response: httpx.Response) -> None:
        self.chunks = response.iter_raw()
        self.buf = b""

    def read(self, count: int) -> list[bytes]:
        parts: list[bytes] = []
        while len(parts) < count:
            part = self._next_part()
            if part is None:
                chunk = next(self.chunks, None)
                if chunk is None:
                    break
                self.buf += chunk
                continue
            parts.append(part)
        return parts

    def _next_part(self) -> bytes | None:
        head_end = self.buf.find(b"\r\n\r\n")
        if head_end < 0:
            return None
        head = self.buf[:head_end].decode("ascii")
        assert head.startswith("--frame"), head
        assert "Content-Type: image/jpeg" in head
        length = int(re.search(r"Content-Length: (\d+)", head).group(1))
        start = head_end + 4
        if len(self.buf) < start + length + 2:
            return None
        part = self.buf[start:start + length]
        self.buf = self.buf[start + length + 2:]
        return part


def parse_multipart(body: bytes) -> list[bytes]:
    parts = []
    for match in re.finditer(rb"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: (\d+)\r\n\r\n", body):
        start = match.end()
        parts.append(body[start:start + int(match.group(1))])
    return parts


def wait_until(check, timeout: float = 10.0, step: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return True
        time.sleep(step)
    return check()


class CountingSource(LiveSource):
    instances: list["CountingSource"] = []

    def __init__(self, fps: float = 50.0, fail_after: int | None = None) -> None:
        self.interval = 1.0 / fps
        self.fail_after = fail_after
        self.closed = threading.Event()
        self.produced = 0
        CountingSource.instances.append(self)

    def frames(self) -> Iterator[bytes]:
        while not self.closed.is_set():
            self.produced += 1
            if self.fail_after is not None and self.produced > self.fail_after:
                raise RuntimeError("device went away")
            yield TINY_JPEG + struct.pack(">I", self.produced)
            self.closed.wait(self.interval)

    def close(self) -> None:
        self.closed.set()


def jpeg_with_marker_bytes_in_segment() -> bytes:
    comment = b"has \xff\xd9 inside"
    segment = b"\xff\xfe" + struct.pack(">H", len(comment) + 2) + comment
    return TINY_JPEG[:2] + segment + TINY_JPEG[2:]


def test_jpeg_splitter_handles_split_chunks_and_garbage() -> None:
    tricky = jpeg_with_marker_bytes_in_segment()
    stream = b"garbage\xff" + TINY_JPEG + b"\x00\x01" + tricky + TINY_JPEG
    rng = random.Random(7)
    splitter = JpegSplitter()
    frames: list[bytes] = []
    pos = 0
    while pos < len(stream):
        step = rng.randint(1, 9)
        frames += splitter.feed(stream[pos:pos + step])
        pos += step
    assert frames == [TINY_JPEG, tricky, TINY_JPEG]
    assert all(is_jpeg(f) for f in frames)


def test_annexb_cutter_only_forwards_complete_nal_units() -> None:
    cutter = AnnexBCutter()
    out = cutter.feed(b"junk\x00\x00\x00\x01\x67AAA\x00\x00\x01\x68BB")
    assert out == b"\x00\x00\x00\x01\x67AAA"
    out = cutter.feed(b"B\x00\x00\x00\x01\x65CC")
    assert out == b"\x00\x00\x01\x68BBB"
    assert cutter.flush() == b"\x00\x00\x00\x01\x65CC"


def test_avcc_parse_and_mov_demux_to_annexb() -> None:
    config = parse_avcc(SIMCTL_AVCC)
    assert config.nal_length_size == 4 and config.sps[0][0] & 0x1F == 7 and config.pps[0][0] & 0x1F == 8
    assert parse_avcc(config.to_bytes()) == config
    sei = b"\x06" + b"\x05" * 10
    idr = b"\x65" + b"\x88" * 30
    slice_ = b"\x41" + b"\x9a" * 12
    samples = b"".join(struct.pack(">I", len(n)) + n for n in (sei, idr, slice_))
    stream = (struct.pack(">I4s", 20, b"ftyp") + b"qt  " + b"\x00" * 8 + struct.pack(">I4s", 8, b"wide")
              + struct.pack(">I4s", 0, b"mdat") + samples)
    demuxer = MovStreamDemuxer(config)
    out = b"".join(demuxer.feed(stream[i:i + 5]) for i in range(0, len(stream), 5))
    assert demuxer.mode == "samples" and demuxer.idr_frames == 1
    expected = (b"\x00\x00\x00\x01" + sei + config.parameter_sets() + b"\x00\x00\x00\x01" + idr
                + b"\x00\x00\x00\x01" + slice_)
    assert out == expected
    with pytest.raises(StreamSyncError):
        MovStreamDemuxer(config).feed(struct.pack(">I4s", 0, b"mdat") + struct.pack(">I", 0))
    passthrough = MovStreamDemuxer(None)
    data = struct.pack(">I4s", 20, b"ftyp") + b"isom" + b"\x00" * 8 + struct.pack(">I4s", 8, b"moof")
    assert passthrough.feed(data) == data and passthrough.mode == "passthrough"


def test_hub_fans_out_one_producer_and_stops_after_the_last_viewer() -> None:
    CountingSource.instances.clear()
    starts = []

    def factory() -> LiveSource:
        starts.append(time.monotonic())
        return CountingSource()

    hub = LiveHub(0.3)
    viewers = [hub.attach("ios:1", factory) for _ in range(3)]
    for viewer in viewers:
        frame = viewer.get(5)
        assert frame is not None and frame.startswith(TINY_JPEG)
    assert len(starts) == 1 and hub.info("ios:1").viewers == 3
    hub.detach(viewers[0])
    hub.detach(viewers[1])
    time.sleep(0.5)
    assert hub.active("ios:1")
    assert viewers[2].get(5) is not None
    hub.detach(viewers[2])
    assert hub.active("ios:1")
    assert wait_until(lambda: hub.info("ios:1") is None, 5)
    assert CountingSource.instances[0].closed.is_set()
    again = hub.attach("ios:1", factory)
    assert again.get(5) is not None and len(starts) == 2
    hub.shutdown()
    assert again.closed and CountingSource.instances[1].closed.is_set()


def test_hub_viewer_returning_in_the_grace_period_keeps_the_producer() -> None:
    CountingSource.instances.clear()
    hub = LiveHub(1.0)
    first = hub.attach("android:1", CountingSource)
    assert first.get(5) is not None
    hub.detach(first)
    time.sleep(0.3)
    second = hub.attach("android:1", CountingSource)
    assert second.get(0.5) is not None
    time.sleep(1.2)
    assert hub.active("android:1") and len(CountingSource.instances) == 1
    hub.shutdown()


def test_hub_failed_producer_closes_viewers_with_the_reason() -> None:
    hub = LiveHub(0.2)
    viewer = hub.attach("ios:2", lambda: CountingSource(fail_after=2))
    assert wait_until(lambda: viewer.closed, 5)
    assert viewer.error == "device went away"
    assert hub.info("ios:2") is None
    hub.shutdown()


def test_hub_suspend_and_resume_keep_viewers_attached() -> None:
    CountingSource.instances.clear()
    hub = LiveHub(5)
    viewer = hub.attach("ios:3", CountingSource)
    assert viewer.get(5) is not None
    assert hub.suspend("ios:3", "video capture")
    assert CountingSource.instances[0].closed.is_set()
    assert not viewer.closed and hub.info("ios:3").state == "suspended"
    late = hub.attach("ios:3", CountingSource)
    assert late.get(0.3) is not None
    assert len(CountingSource.instances) == 1
    assert hub.resume("ios:3")
    assert viewer.get(5) is not None and len(CountingSource.instances) == 2
    hub.shutdown()


def test_device_live_fans_out_over_http_and_stops_after_viewers_leave(config: Config) -> None:
    config.set("live.idleStopSeconds", 1)
    app = make_app(config)
    ctx = app.state.ctx
    ctx.pools.devices.boot("ios:1")
    created = []
    original = ctx.pools.devices.live_source

    def tracking(key: str):
        source = original(key)
        created.append(source)
        return source

    ctx.pools.devices.live_source = tracking
    with running(app) as base:
        hub = routes_live.live_hub(ctx)
        with httpx.Client(timeout=10) as one, httpx.Client(timeout=10) as two:
            with one.stream("GET", f"{base}/api/devices/ios/1/live") as first:
                assert first.status_code == 200
                assert first.headers["content-type"].startswith("multipart/x-mixed-replace; boundary=frame")
                assert first.headers["cache-control"].startswith("no-store")
                first_parts = PartReader(first)
                assert first_parts.read(3) == [TINY_JPEG] * 3
                with two.stream("GET", f"{base}/api/devices/ios/1/live") as second:
                    assert PartReader(second).read(3) == [TINY_JPEG] * 3
                    assert hub.info("ios:1").viewers == 2
                    assert len(created) == 1
                assert wait_until(lambda: hub.info("ios:1").viewers == 1, 5)
                assert first_parts.read(2) == [TINY_JPEG] * 2
        assert wait_until(lambda: hub.info("ios:1") is not None and hub.info("ios:1").viewers == 0, 5)
        assert hub.active("ios:1")
        assert wait_until(lambda: hub.info("ios:1") is None, 5)
        assert created[0]._stop.is_set()


def test_profile_live_and_readiness_errors(client: TestClient, app: FastAPI) -> None:
    ctx = app.state.ctx
    assert client.get("/api/profiles/browser:1:1/live?frames=1").status_code == 409
    ctx.pools.browsers.ensure(1)
    response = client.get("/api/profiles/browser%3A1%3A1/live", params={"frames": 2})
    assert response.status_code == 200
    assert parse_multipart(response.content) == [TINY_JPEG, TINY_JPEG]
    assert client.get("/api/profiles/1:1/live?frames=1").status_code == 200
    missing = client.get("/api/profiles/browser:9:1/live")
    assert missing.status_code == 404 and missing.json()["error"] == "profile_not_found"
    off = client.get("/api/devices/android/1/live")
    assert off.status_code == 409 and off.json()["error"] == "device_not_running"
    assert client.get("/api/devices/ios/99/live").status_code == 404
    assert client.get("/api/devices/tvos/1/live").status_code == 422


def _hold(env: AuthEnv, resource: str, session_id: int | None = None, owner_user: int | None = None,
          kind: str = "ios") -> leases_repo.Lease:
    with env.db.transaction() as conn:
        return leases_repo.insert(conn, kind=kind, owner_instance="test", state="active", resource=resource,
                                  session_id=session_id, owner_kind="manual" if owner_user else "agent",
                                  owner_user=owner_user)


def test_live_access_needs_a_viewer_grant_on_the_holding_session(auth_env: AuthEnv) -> None:
    env = auth_env
    env.app.state.ctx.pools.devices.boot("ios:1")
    url = "/api/devices/ios/1/live?frames=1"
    assert env.client.get(url).status_code == 401
    viewer, viewer_key = env.make_user("viewer")
    other, other_key = env.make_user("other")
    stranger, stranger_key = env.make_user("stranger")
    unleased = env.client.get(url, headers=bearer(viewer_key))
    assert unleased.status_code == 403 and unleased.json()["error"] == "live_forbidden"
    assert env.client.get(url, headers=env.admin_headers).status_code == 200
    session = ensure_session(env.db, "acme/mobile-app", "feature/live")
    ensure_session(env.db, "acme/mobile-app", "feature/other")
    _hold(env, "ios:1", session_id=session.id)
    env.grant(viewer, "acme/mobile-app", "viewer", session_name="feature/live")
    env.grant(other, "acme/mobile-app", "viewer", session_name="feature/other")
    allowed = env.client.get(url, headers=bearer(viewer_key))
    assert allowed.status_code == 200 and parse_multipart(allowed.content) == [TINY_JPEG]
    assert env.client.get(url, headers=bearer(other_key)).status_code == 403
    assert env.client.get(url, headers=bearer(stranger_key)).status_code == 404
    env.grant(stranger, "acme/mobile-app", "viewer")
    assert env.client.get(url, headers=bearer(stranger_key)).status_code == 200


def test_manual_lease_owner_may_watch(auth_env: AuthEnv) -> None:
    env = auth_env
    env.app.state.ctx.pools.devices.boot("android:1")
    owner, owner_key = env.make_user("owner")
    _hold(env, "android:1", owner_user=owner.id, kind="android")
    assert env.client.get("/api/devices/android/1/live?frames=1", headers=bearer(owner_key)).status_code == 200


def test_stream_ends_when_the_viewer_loses_access(auth_config: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(routes_live, "RECHECK_SECONDS", 0.2)
    app = make_app(auth_config)
    ctx = app.state.ctx
    ctx.pools.devices.boot("ios:2")
    member = create_user(ctx.db, "member")
    key = create_key(ctx.db, member.id)
    session = ensure_session(ctx.db, "acme/mobile-app", "feature/revoke")
    add_grant(ctx.db, member.id, "acme/mobile-app", "viewer", session_name="feature/revoke")
    with ctx.db.transaction() as conn:
        lease = leases_repo.insert(conn, kind="ios", owner_instance="test", state="active", resource="ios:2",
                                   session_id=session.id)
    with running(app) as base, httpx.Client(timeout=10) as http:
        with http.stream("GET", f"{base}/api/devices/ios/2/live", headers=bearer(key)) as response:
            assert response.status_code == 200
            reader = PartReader(response)
            assert len(reader.read(2)) == 2
            with ctx.db.transaction() as conn:
                leases_repo.end(conn, lease.id, "released", "test")
            started = time.monotonic()
            remaining = reader.read(10_000)
            assert time.monotonic() - started < 5
            assert len(remaining) < 100


def test_profile_contexts_come_from_the_holding_lease(auth_env: AuthEnv) -> None:
    env = auth_env
    pools = env.app.state.ctx.pools
    target = profile_target(pools, "browser:1:1")
    with pytest.raises(LiveUnavailable) as unheld:
        profile_contexts(env.db, target)
    assert unheld.value.status == 409
    session = ensure_session(env.db, "acme/web-app", "feature/x")
    with env.db.transaction() as conn:
        first = leases_repo.insert(conn, kind="browser", owner_instance="a", state="active", resource="browser:1:1",
                                   session_id=session.id)
    assert profile_contexts(env.db, target) is None
    with env.db.transaction() as conn:
        leases_repo.insert(conn, kind="browser", owner_instance="b", state="active", resource="browser:1:2",
                           session_id=session.id)
    with pytest.raises(LiveUnavailable) as ambiguous:
        profile_contexts(env.db, target)
    assert ambiguous.value.error == "profile_contexts_unknown"
    with env.db.transaction() as conn:
        leases_repo.update(conn, first.id, meta={"browserContextIds": ["CTX1"]})
    assert profile_contexts(env.db, target) == ("CTX1",)


def h264_bytes(seconds: float, size: str = "160x120", rate: int = 10) -> bytes:
    return subprocess.run(
        [FFMPEG, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", f"testsrc=size={size}:rate={rate}",
         "-t", str(seconds), "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-f", "h264", "-"],
        check=True, capture_output=True).stdout


@needs_ffmpeg
def test_transcoder_turns_h264_into_jpeg_frames() -> None:
    transcoder = MjpegTranscoder("h264", StreamSettings(quality=60), name="test ffmpeg")
    try:
        transcoder.write(h264_bytes(1.0))
        transcoder.end_input()
        frames: list[bytes] = []
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and not transcoder.ended:
            frame = transcoder.next_frame(0.5)
            if frame is not None:
                frames.append(frame)
        assert len(frames) >= 8
        assert all(is_jpeg(f) for f in frames)
    finally:
        transcoder.close()


@needs_ffmpeg
def test_android_source_restarts_screenrecord_segments() -> None:
    def command(seconds: int) -> list[str]:
        return [FFMPEG, "-hide_banner", "-loglevel", "error", "-re", "-f", "lavfi", "-i",
                "testsrc=size=160x120:rate=10", "-t", str(seconds), "-c:v", "libx264", "-preset", "ultrafast",
                "-tune", "zerolatency", "-pix_fmt", "yuv420p", "-f", "h264", "-"]

    source = AndroidLiveSource("emulator-5580", StreamSettings(), name="Android test", segment_seconds=1,
                               command=command)
    frames = []
    collector = threading.Thread(target=lambda: _take(source.frames(), 30, 20, frames), daemon=True)
    collector.start()
    collector.join(25)
    source.close()
    assert len(frames) >= 30
    assert source.segments >= 2
    assert all(is_jpeg(f) for f in frames)


def _take(iterator, count: int, timeout: float, out: list[bytes] | None = None) -> list[bytes]:
    out = [] if out is None else out
    deadline = time.monotonic() + timeout
    for frame in iterator:
        out.append(frame)
        if len(out) >= count or time.monotonic() > deadline:
            break
    return out


FAKE_SIMCTL = textwrap.dedent("""
    import os, signal, stat, sys, time
    target, source, mode = sys.argv[1], sys.argv[2], sys.argv[3]
    data = open(source, "rb").read()
    stop = {"now": False}
    signal.signal(signal.SIGINT, lambda *_: stop.update(now=True))
    pos, moov_at = 0, None
    while pos < len(data):
        size = int.from_bytes(data[pos:pos + 4], "big")
        if data[pos + 4:pos + 8] == b"moov":
            moov_at = pos
        pos += size
    streaming = data[:moov_at]
    print("Recording started", file=sys.stderr, flush=True)
    if not os.path.basename(target).startswith("live-"):
        while not stop["now"]:
            time.sleep(0.02)
        open(target, "wb").write(data)
        sys.exit(0)
    if mode == "replace" and os.path.exists(target) and stat.S_ISFIFO(os.stat(target).st_mode):
        os.unlink(target)
    final = target
    if mode == "staging":
        if os.path.exists(target):
            os.unlink(target)
        open(target, "wb").close()
        target = target + ".sb-22f710ee-UhJgCp"
    out = open(target, "wb", buffering=0)
    for i in range(0, len(streaming), 1024):
        if stop["now"]:
            break
        out.write(streaming[i:i + 1024])
        time.sleep(0.04)
    while not stop["now"]:
        time.sleep(0.02)
    if final != target:
        os.replace(target, final)
    sys.exit(0)
""")


def _mov_with_trailing_moov(tmp_path: Path) -> Path:
    path = tmp_path / "source.mov"
    subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                    "testsrc=size=160x120:rate=10", "-t", "3", "-c:v", "libx264", "-preset", "ultrafast",
                    "-pix_fmt", "yuv420p", "-f", "mov", str(path)], check=True)
    return path


def _ios_source(tmp_path: Path, udid: str, mode: str) -> IosLiveSource:
    script = tmp_path / "fake_simctl.py"
    script.write_text(FAKE_SIMCTL, encoding="utf-8")
    source_mov = _mov_with_trailing_moov(tmp_path)
    assert find_avcc(source_mov.read_bytes()) is not None

    def recorder(target: Path) -> list[str]:
        return [sys.executable, str(script), str(target), str(source_mov), mode]

    return IosLiveSource(udid, StreamSettings(), tmp_path / "work", name=f"iOS {udid}", recorder=recorder)


@needs_ffmpeg
@pytest.mark.skipif(sys.platform == "win32", reason="FIFOs are POSIX only")
def test_ios_source_primes_codec_config_and_streams_through_a_fifo(tmp_path: Path) -> None:
    source = _ios_source(tmp_path, "FAKE-UDID-FIFO", "fifo")
    frames = []
    collector = threading.Thread(target=lambda: _take(source.frames(), 10, 20, frames), daemon=True)
    collector.start()
    collector.join(30)
    source.close()
    assert len(frames) >= 10 and all(is_jpeg(f) for f in frames)
    assert source.transport == "fifo"
    assert isinstance(ios_module.cached_config("FAKE-UDID-FIFO"), AvcConfig)
    assert wait_until(lambda: not any((tmp_path / "work").iterdir()), 30)


@needs_ffmpeg
@pytest.mark.skipif(sys.platform == "win32", reason="FIFOs are POSIX only")
def test_ios_source_reads_the_growing_file_when_simctl_replaces_the_fifo(tmp_path: Path) -> None:
    source = _ios_source(tmp_path, "FAKE-UDID-FILE", "replace")
    frames = []
    collector = threading.Thread(target=lambda: _take(source.frames(), 10, 20, frames), daemon=True)
    collector.start()
    collector.join(30)
    source.close()
    assert len(frames) >= 10 and all(is_jpeg(f) for f in frames)
    assert source.transport == "file"
    assert ios_module.fifo_rejection("FAKE-UDID-FILE")


@needs_ffmpeg
@pytest.mark.skipif(sys.platform == "win32", reason="FIFOs are POSIX only")
def test_ios_source_follows_the_staging_file_simctl_writes_before_renaming(tmp_path: Path) -> None:
    source = _ios_source(tmp_path, "FAKE-UDID-STAGING", "staging")
    frames = []
    collector = threading.Thread(target=lambda: _take(source.frames(), 10, 20, frames), daemon=True)
    collector.start()
    collector.join(30)
    source.close()
    assert len(frames) >= 10 and all(is_jpeg(f) for f in frames)
    assert source.transport == "file"


class FakeChrome:
    def __init__(self) -> None:
        self.targets = [
            {"targetId": "DEFAULT", "type": "page", "url": "about:blank", "browserContextId": "DEF"},
            {"targetId": "A", "type": "page", "url": "https://a.test/", "browserContextId": "CTX1"},
            {"targetId": "B", "type": "page", "url": "https://b.test/", "browserContextId": "CTX2"},
        ]
        self.acks = 0
        self.attached: list[str] = []
        self.connections = []
        self.frames_by_session: dict[str, int] = {}
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.port = free_port()
        self.server = serve(self._handler, "127.0.0.1", self.port, process_request=self._http)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def _http(self, connection, request):
        if request.path == "/json/version":
            body = json.dumps({"webSocketDebuggerUrl": f"ws://127.0.0.1:{self.port}/devtools/browser/x"})
            return connection.respond(200, body)
        return None

    def send(self, connection, message: dict) -> None:
        with self.lock:
            connection.send(json.dumps(message))

    def _screencast(self, connection, session: str) -> None:
        counter = 0
        while not self.stop.is_set() and session in self.frames_by_session:
            counter += 1
            try:
                self.send(connection, {"method": "Page.screencastFrame", "sessionId": session,
                                       "params": {"data": base64.b64encode(TINY_JPEG).decode(),
                                                  "metadata": {}, "sessionId": counter}})
            except Exception:
                return
            self.frames_by_session[session] = counter
            time.sleep(0.03)

    def _handler(self, connection) -> None:
        self.connections.append(connection)
        for raw in connection:
            message = json.loads(raw)
            method, params, reply = message["method"], message.get("params", {}), {}
            if method == "Target.getBrowserContexts":
                reply = {"browserContextIds": ["CTX1", "CTX2"]}
            elif method == "Target.getTargets":
                reply = {"targetInfos": self.targets}
            elif method == "Target.attachToTarget":
                self.attached.append(params["targetId"])
                reply = {"sessionId": f"S-{params['targetId']}"}
            elif method == "Page.startScreencast":
                self.frames_by_session[message["sessionId"]] = 0
                threading.Thread(target=self._screencast, args=(connection, message["sessionId"]),
                                 daemon=True).start()
            elif method == "Page.stopScreencast":
                self.frames_by_session.pop(message.get("sessionId"), None)
            elif method == "Page.screencastFrameAck":
                self.acks += 1
                continue
            self.send(connection, {"id": message["id"], "result": reply})

    def event(self, method: str, params: dict) -> None:
        for connection in self.connections:
            self.send(connection, {"method": method, "params": params})

    def close(self) -> None:
        self.stop.set()
        self.server.shutdown()


def test_chrome_screencast_follows_the_profile_page_and_acks_frames() -> None:
    chrome = FakeChrome()
    source = ChromeScreencastSource(chrome.url, StreamSettings(), name="Chrome 1 profile 2", context_ids=["CTX2"])
    frames: list[bytes] = []
    iterator = source.frames()
    try:
        frames += _take(iterator, 5, 10)
        assert frames == [TINY_JPEG] * 5
        assert chrome.attached == ["B"] and source.target_id == "B"
        assert wait_until(lambda: chrome.acks >= 4, 5)
        chrome.event("Target.targetCreated", {"targetInfo": {"targetId": "C", "type": "page", "url": "https://c.test/",
                                                             "browserContextId": "CTX2"}})
        chrome.event("Target.targetCreated", {"targetInfo": {"targetId": "D", "type": "page", "url": "https://d.test/",
                                                             "browserContextId": "CTX1"}})
        frames += _take(iterator, 5, 10)
        assert source.target_id == "C" and chrome.attached == ["B", "C"]
        chrome.event("Target.targetDestroyed", {"targetId": "C"})
        frames += _take(iterator, 3, 10)
        assert source.target_id == "B" and chrome.attached == ["B", "C", "B"]
    finally:
        source.close()
        iterator.close()
        chrome.close()


def test_chrome_screencast_without_reported_contexts_skips_the_default_window() -> None:
    chrome = FakeChrome()
    chrome.targets = chrome.targets[:2]
    source = ChromeScreencastSource(chrome.url, StreamSettings(), name="Chrome 1 profile 1")
    iterator = source.frames()
    try:
        assert _take(iterator, 2, 10) == [TINY_JPEG] * 2
        assert chrome.attached == ["A"]
    finally:
        source.close()
        iterator.close()
        chrome.close()


def test_chrome_screencast_with_stale_contexts_fails_clearly_when_shared() -> None:
    chrome = FakeChrome()
    source = ChromeScreencastSource(chrome.url, StreamSettings(), name="Chrome 1 profile 1", context_ids=["GONE"])
    iterator = source.frames()
    try:
        with pytest.raises(LiveError, match="no longer exist"):
            _take(iterator, 1, 10)
    finally:
        source.close()
        chrome.close()


def test_chrome_screencast_with_stale_contexts_shows_any_page_when_exclusive() -> None:
    chrome = FakeChrome()
    chrome.targets = chrome.targets[:2]
    source = ChromeScreencastSource(chrome.url, StreamSettings(), name="Chrome 1 profile 1", context_ids=["GONE"],
                                    exclusive=True)
    iterator = source.frames()
    try:
        assert _take(iterator, 2, 10) == [TINY_JPEG] * 2
        assert chrome.attached
    finally:
        source.close()
        iterator.close()
        chrome.close()
