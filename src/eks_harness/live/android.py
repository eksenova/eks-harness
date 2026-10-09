from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Iterator, Sequence

from eks_harness.live.common import LiveError, StreamSettings, adb_binary
from eks_harness.live.h264 import AnnexBCutter
from eks_harness.live.process import READ_CHUNK, ManagedProcess, MjpegTranscoder
from eks_harness.pools.base import LiveSource

log = logging.getLogger("eks_harness.live")

SEGMENT_SECONDS = 170
SEGMENT_OVERRUN_SECONDS = 10
MAX_FAILED_SEGMENTS = 3
BIT_RATE = 8_000_000

CommandFactory = Callable[[int], Sequence[str]]


def screenrecord_command(adb: str, serial: str, seconds: int) -> list[str]:
    return [adb, "-s", serial, "exec-out", "screenrecord", "--output-format=h264", f"--time-limit={seconds}",
            f"--bit-rate={BIT_RATE}", "-"]


class AndroidLiveSource(LiveSource):
    def __init__(self, serial: str, settings: StreamSettings, *, name: str | None = None, adb: str | None = None,
                 segment_seconds: int = SEGMENT_SECONDS, command: CommandFactory | None = None,
                 ffmpeg: str | None = None) -> None:
        self.serial = serial
        self.settings = settings
        self.name = name or serial
        self.segment_seconds = int(segment_seconds)
        self.segments = 0
        self._ffmpeg = ffmpeg
        if command is None:
            binary = adb or adb_binary()
            if not binary:
                raise LiveError("adb was not found (set ANDROID_HOME); the Android live view needs it")
            command = lambda seconds: screenrecord_command(binary, serial, seconds)
        self._command = command
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._process: ManagedProcess | None = None
        self._transcoder: MjpegTranscoder | None = None
        self._feeder: threading.Thread | None = None
        self._error: str | None = None

    def frames(self) -> Iterator[bytes]:
        transcoder = MjpegTranscoder("h264", self.settings, name=f"ffmpeg {self.name}", ffmpeg=self._ffmpeg)
        with self._lock:
            self._transcoder = transcoder
            if self._stop.is_set():
                transcoder.close()
                return
        self._feeder = threading.Thread(target=self._feed, args=(transcoder,), daemon=True,
                                        name=f"live-{self.name}-screenrecord")
        self._feeder.start()
        try:
            yield from transcoder.frames(self._stop)
        except LiveError:
            if self._error and not self._stop.is_set():
                raise LiveError(self._error) from None
            raise
        finally:
            self.close()
        if self._error:
            raise LiveError(self._error)

    def _feed(self, transcoder: MjpegTranscoder) -> None:
        failures = 0
        try:
            while not self._stop.is_set():
                produced, detail = self._segment(transcoder)
                if self._stop.is_set():
                    break
                if produced:
                    failures = 0
                    continue
                failures += 1
                if failures >= MAX_FAILED_SEGMENTS:
                    self._error = f"screenrecord on {self.name} produced no video: {detail}"
                    break
                self._stop.wait(1.0)
        except LiveError as error:
            if not self._stop.is_set():
                self._error = str(error)
        except Exception as error:
            if not self._stop.is_set():
                self._error = f"the {self.name} screen stream failed: {error}"
                log.exception("android live feeder failed")
        finally:
            transcoder.end_input()

    def _segment(self, transcoder: MjpegTranscoder) -> tuple[int, str]:
        process = ManagedProcess(self._command(self.segment_seconds), name=f"screenrecord {self.name}")
        with self._lock:
            self._process = process
            if self._stop.is_set():
                process.stop(grace=2.0)
                return 0, "stopped"
        self.segments += 1
        cutter = AnnexBCutter()
        produced = 0
        deadline = time.monotonic() + self.segment_seconds + SEGMENT_OVERRUN_SECONDS
        overrun = threading.Timer(max(1.0, deadline - time.monotonic()), process.stop, kwargs={"grace": 3.0})
        overrun.daemon = True
        overrun.start()
        stream = process.proc.stdout
        try:
            while not self._stop.is_set():
                chunk = stream.read(READ_CHUNK) if stream is not None else b""
                if not chunk:
                    break
                produced += len(chunk)
                transcoder.write(cutter.feed(chunk))
            code = process.wait(5)
            if not process.stopping and not self._stop.is_set() and code is not None:
                transcoder.write(cutter.flush())
        except (OSError, ValueError):
            pass
        finally:
            overrun.cancel()
            process.stop(grace=2.0)
            with self._lock:
                if self._process is process:
                    self._process = None
        return produced, process.describe_exit()

    def close(self) -> None:
        self._stop.set()
        with self._lock:
            process, transcoder = self._process, self._transcoder
        if process is not None:
            process.stop(grace=2.0)
        if transcoder is not None:
            transcoder.close()
        feeder = self._feeder
        if feeder is not None and feeder is not threading.current_thread():
            feeder.join(5)
