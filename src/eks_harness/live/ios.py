from __future__ import annotations

import errno
import logging
import os
import select
import shutil
import signal
import stat
import tempfile
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path

from eks_harness.live.common import LiveError, StreamSettings, xcrun_binary
from eks_harness.live.h264 import AvcConfig, MovStreamDemuxer, StreamSyncError, avcc_from_file
from eks_harness.live.process import READ_CHUNK, ManagedProcess, MjpegTranscoder, watchdog_command
from eks_harness.pools.base import LiveSource

log = logging.getLogger("eks_harness.live")

RECORDING_STARTED = "Recording started"
START_TIMEOUT = 20.0
PRIME_HOLD = 0.6
STOP_GRACE = 20.0
NO_WRITER_TIMEOUT = 20.0
FILE_ROTATE_BYTES = 512 * 1024 * 1024
DECODE_CHECK_SECONDS = 8.0

RecorderCommand = Callable[[Path], Sequence[str]]

_config_cache: dict[str, AvcConfig] = {}
_config_lock = threading.Lock()
_fifo_unsupported: dict[str, str] = {}


def simctl_record_command(xcrun: str, udid: str) -> RecorderCommand:
    return lambda target: [xcrun, "simctl", "io", udid, "recordVideo", "--codec=h264", "--force", str(target)]


def cached_config(udid: str) -> AvcConfig | None:
    with _config_lock:
        return _config_cache.get(udid)


def forget_config(udid: str) -> None:
    with _config_lock:
        _config_cache.pop(udid, None)


def fifo_rejection(udid: str) -> str | None:
    with _config_lock:
        return _fifo_unsupported.get(udid)


class FifoRejected(LiveError):
    pass


class _Segment:
    def __init__(self, path: Path, transport: str, fd: int | None) -> None:
        self.path = path
        self.transport = transport
        self.fd = fd
        self.fifo_inode = os.fstat(fd).st_ino if fd is not None else None
        self.process: ManagedProcess | None = None
        self.opened = time.monotonic()
        self.bytes = 0


class IosLiveSource(LiveSource):
    def __init__(self, udid: str, settings: StreamSettings, work_root: Path, *, name: str | None = None,
                 recorder: RecorderCommand | None = None, ffmpeg: str | None = None,
                 rotate_bytes: int = FILE_ROTATE_BYTES, use_fifo: bool | None = None) -> None:
        self.udid = udid
        self.settings = settings
        self.name = name or udid
        self.work_root = Path(work_root)
        self.rotate_bytes = rotate_bytes
        self.transport: str | None = None
        self.segments = 0
        self._ffmpeg = ffmpeg
        if recorder is None:
            xcrun = xcrun_binary()
            if not xcrun:
                raise LiveError("xcrun was not found; the iOS live view needs Xcode on macOS")
            recorder = simctl_record_command(xcrun, udid)
        self._recorder = recorder
        self._use_fifo = use_fifo
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._segment: _Segment | None = None
        self._prime: ManagedProcess | None = None
        self._transcoder: MjpegTranscoder | None = None
        self._transcoder_format: str | None = None
        self._work: Path | None = None
        self._error: str | None = None
        self._first_idr: float | None = None
        self._frames_before = 0

    def _start_recorder(self, target: Path, name: str) -> ManagedProcess:
        return ManagedProcess(watchdog_command(self._recorder(target), grace=STOP_GRACE), name=name, stdout=False)

    def _stop_recorder(self, process: ManagedProcess) -> None:
        process.stop(grace=STOP_GRACE + 5, first=signal.SIGTERM, group=False, kill=False)
        if process.alive():
            log.warning("the %s recorder did not stop after SIGINT; its watchdog keeps trying", self.name)

    def _codec_config(self) -> AvcConfig:
        found = cached_config(self.udid)
        if found is not None:
            return found
        target = self._work / "prime.mov"
        process = self._start_recorder(target, f"simctl prime {self.name}")
        with self._lock:
            self._prime = process
        try:
            if not process.wait_for_stderr(RECORDING_STARTED, START_TIMEOUT):
                raise LiveError(f"simctl did not start recording {self.name}: {process.describe_exit()}")
            self._stop.wait(PRIME_HOLD)
        finally:
            self._stop_recorder(process)
            with self._lock:
                self._prime = None
        if self._stop.is_set():
            raise LiveError("stopped")
        try:
            config = avcc_from_file(target) if target.exists() else None
        except (OSError, ValueError) as error:
            raise LiveError(f"could not read the codec configuration simctl wrote for {self.name}: {error}") from error
        finally:
            target.unlink(missing_ok=True)
        if config is None:
            raise LiveError(f"simctl wrote no H.264 codec configuration for {self.name}: {process.describe_exit()}")
        with _config_lock:
            _config_cache[self.udid] = config
        return config

    def _want_fifo(self) -> bool:
        if self._use_fifo is not None:
            return self._use_fifo
        return hasattr(os, "mkfifo") and fifo_rejection(self.udid) is None

    def frames(self) -> Iterator[bytes]:
        self.work_root.mkdir(parents=True, exist_ok=True)
        self._work = Path(tempfile.mkdtemp(prefix="ios-", dir=self.work_root))
        feeder = None
        try:
            config = self._codec_config()
            feeder = threading.Thread(target=self._feed, args=(config,), daemon=True, name=f"live-{self.name}-feed")
            feeder.start()
            yield from self._consume(feeder)
        finally:
            self._stop.set()
            if feeder is not None and feeder is not threading.current_thread():
                feeder.join(STOP_GRACE + 10)
            with self._lock:
                transcoder, self._transcoder = self._transcoder, None
            if transcoder is not None:
                transcoder.close()
            shutil.rmtree(self._work, ignore_errors=True)

    def _consume(self, feeder: threading.Thread) -> Iterator[bytes]:
        while not self._stop.is_set():
            with self._lock:
                transcoder = self._transcoder
            if transcoder is None:
                if not feeder.is_alive():
                    break
                self._stop.wait(0.1)
                continue
            frame = transcoder.next_frame(0.25)
            if frame is not None:
                yield frame
                continue
            if (self._first_idr is not None and transcoder.frames_out == self._frames_before
                    and time.monotonic() - self._first_idr > DECODE_CHECK_SECONDS):
                forget_config(self.udid)
                raise LiveError(f"the {self.name} stream could not be decoded; the codec configuration was dropped "
                                f"and is read again on the next start")
            if transcoder.ended:
                with self._lock:
                    replaced = self._transcoder is not transcoder
                if replaced:
                    continue
                if not feeder.is_alive() or transcoder.process.returncode() is not None:
                    break
        if self._stop.is_set():
            return
        feeder.join(5)
        if self._error:
            raise LiveError(self._error)
        with self._lock:
            transcoder = self._transcoder
        if transcoder is not None and transcoder.process.returncode() not in (None, 0):
            raise LiveError(f"ffmpeg ended the {self.name} stream: {transcoder.process.describe_exit()}")
        raise LiveError(f"the {self.name} stream ended")

    def _feed(self, config: AvcConfig) -> None:
        try:
            self._feed_segments(config)
        except LiveError as error:
            if not self._stop.is_set():
                self._error = str(error)
        except Exception as error:
            if not self._stop.is_set():
                self._error = f"the {self.name} screen stream failed: {error}"
                log.exception("iOS live feeder failed")

    def _feed_segments(self, config: AvcConfig) -> None:
        index = 0
        fifo = self._want_fifo()
        rejection: str | None = None
        while not self._stop.is_set():
            index += 1
            demuxer = MovStreamDemuxer(config)
            segment = self._open_segment(index, fifo)
            started = time.monotonic()
            with self._lock:
                self._frames_before = self._transcoder.frames_out if self._transcoder is not None else 0
            self._first_idr = None
            try:
                for chunk in self._read_segment(segment):
                    try:
                        data = demuxer.feed(chunk)
                    except StreamSyncError as error:
                        raise LiveError(f"the {self.name} recording could not be parsed: {error}") from error
                    if data:
                        self._ensure_transcoder(demuxer.mode).write(data)
                        if rejection is not None:
                            with _config_lock:
                                _fifo_unsupported[self.udid] = rejection
                            rejection = None
                    if demuxer.idr_frames and self._first_idr is None:
                        self._first_idr = time.monotonic()
            except FifoRejected as rejected:
                if self._stop.is_set():
                    return
                rejection = str(rejected)
                log.warning("simctl did not record %s into a FIFO (%s); reading its growing recording file instead",
                            self.name, rejection)
                fifo = False
                continue
            finally:
                self._close_segment(segment)
            if self._stop.is_set():
                return
            if segment.transport == "file" and segment.bytes >= self.rotate_bytes:
                if self._transcoder_format == "mov":
                    self._reset_transcoder()
                continue
            if segment.bytes == 0:
                raise LiveError(f"simctl recorded nothing from {self.name}: {self._exit(segment)}")
            if time.monotonic() - started < 2:
                raise LiveError(f"the {self.name} recording stopped right away: {self._exit(segment)}")
            raise LiveError(f"the {self.name} recording ended: {self._exit(segment)}")

    @staticmethod
    def _exit(segment: _Segment) -> str:
        return segment.process.describe_exit() if segment.process is not None else "the recorder did not start"

    def _ensure_transcoder(self, mode: str) -> MjpegTranscoder:
        with self._lock:
            if self._transcoder is None:
                if self._stop.is_set():
                    raise LiveError("stopped")
                input_format = "mov" if mode == "passthrough" else "h264"
                self._transcoder = MjpegTranscoder(input_format, self.settings, name=f"ffmpeg {self.name}",
                                                   ffmpeg=self._ffmpeg)
                self._transcoder_format = input_format
            return self._transcoder

    def _reset_transcoder(self) -> None:
        with self._lock:
            transcoder, self._transcoder = self._transcoder, None
            self._transcoder_format = None
        if transcoder is not None:
            transcoder.end_input()
            transcoder.process.wait(5)
            transcoder.close()

    def _open_segment(self, index: int, fifo: bool) -> _Segment:
        path = self._work / f"live-{index}.mov"
        path.unlink(missing_ok=True)
        fd = None
        if fifo:
            os.mkfifo(path, 0o600)
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        segment = _Segment(path, "fifo" if fifo else "file", fd)
        with self._lock:
            self._segment = segment
            stopped = self._stop.is_set()
        if not stopped:
            segment.process = self._start_recorder(path, f"simctl live {self.name}")
        if self._stop.is_set():
            self._close_segment(segment)
            raise LiveError("stopped")
        self.segments += 1
        self.transport = segment.transport
        return segment

    def _close_segment(self, segment: _Segment) -> None:
        with self._lock:
            if self._segment is segment:
                self._segment = None
        drained = threading.Event()
        drainer = None
        if segment.fd is not None and segment.process is not None:
            drainer = threading.Thread(target=self._discard, args=(segment, drained), daemon=True,
                                       name=f"live-{self.name}-drain")
            drainer.start()
        if segment.process is not None:
            self._stop_recorder(segment.process)
        drained.set()
        if drainer is not None:
            drainer.join(2)
        if segment.fd is not None:
            try:
                os.close(segment.fd)
            except OSError:
                pass
            segment.fd = None
        try:
            segment.path.unlink(missing_ok=True)
        except OSError:
            pass

    @staticmethod
    def _discard(segment: _Segment, done: threading.Event) -> None:
        fd = segment.fd
        while not done.is_set():
            try:
                ready, _, _ = select.select([fd], [], [], 0.1)
                if ready and not os.read(fd, READ_CHUNK):
                    if not segment.process.alive():
                        return
                    done.wait(0.05)
            except BlockingIOError:
                continue
            except (OSError, ValueError):
                return

    def _read_segment(self, segment: _Segment) -> Iterator[bytes]:
        if segment.transport == "fifo":
            yield from self._read_fifo(segment)
        else:
            yield from self._read_file(segment)

    def _read_fifo(self, segment: _Segment) -> Iterator[bytes]:
        fd = segment.fd
        while not self._stop.is_set():
            ready, _, _ = select.select([fd], [], [], 0.25)
            if not ready:
                if not segment.bytes:
                    self._check_waiting(segment)
                elif not segment.process.alive():
                    return
                continue
            try:
                chunk = os.read(fd, READ_CHUNK)
            except BlockingIOError:
                continue
            except OSError as error:
                if error.errno == errno.EAGAIN:
                    continue
                raise LiveError(f"reading the {self.name} recording failed: {error}") from error
            if chunk:
                segment.bytes += len(chunk)
                yield chunk
                continue
            if segment.bytes:
                return
            self._check_waiting(segment)
            self._stop.wait(0.05)

    def _check_waiting(self, segment: _Segment) -> None:
        try:
            current = os.stat(segment.path)
        except FileNotFoundError:
            current = None
        if current is None or current.st_ino != segment.fifo_inode or not stat.S_ISFIFO(current.st_mode):
            raise FifoRejected("simctl replaced the FIFO with a regular file")
        if not segment.process.alive():
            raise FifoRejected(segment.process.describe_exit())
        if time.monotonic() - segment.opened > NO_WRITER_TIMEOUT:
            raise FifoRejected(f"simctl did not write to the FIFO within {NO_WRITER_TIMEOUT:.0f}s")

    def _read_file(self, segment: _Segment) -> Iterator[bytes]:
        handle = None
        try:
            while not self._stop.is_set():
                if handle is None:
                    try:
                        handle = open(segment.path, "rb")
                    except FileNotFoundError:
                        if not segment.process.alive():
                            return
                        if time.monotonic() - segment.opened > NO_WRITER_TIMEOUT:
                            raise LiveError(f"simctl did not create the {self.name} recording: "
                                            f"{segment.process.describe_exit()}") from None
                        self._stop.wait(0.1)
                        continue
                chunk = handle.read(READ_CHUNK)
                if chunk:
                    segment.bytes += len(chunk)
                    yield chunk
                    if segment.bytes >= self.rotate_bytes:
                        return
                    continue
                if not segment.process.alive():
                    return
                self._stop.wait(0.05)
        finally:
            if handle is not None:
                handle.close()

    def close(self) -> None:
        self._stop.set()
        with self._lock:
            segment, prime, transcoder = self._segment, self._prime, self._transcoder
        if prime is not None:
            prime.send_signal(signal.SIGTERM, group=False)
        if segment is not None and segment.process is not None:
            segment.process.send_signal(signal.SIGTERM, group=False)
        if transcoder is not None:
            transcoder.close()
