from __future__ import annotations

import logging
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from collections.abc import Iterator, Sequence

from eks_harness.live.common import LiveError, StreamSettings, ffmpeg_binary
from eks_harness.live.jpeg import JpegSplitter

log = logging.getLogger("eks_harness.live")

WINDOWS = sys.platform == "win32"
READ_CHUNK = 1 << 16
ACCESS_UNIT_DELIMITER = b"\x00\x00\x00\x01\x09\xf0"
AUD_DELAY = 0.03


class ManagedProcess:
    def __init__(self, args: Sequence[str], *, name: str, stdin: bool = False, stdout: bool = True,
                 env: dict[str, str] | None = None, stderr_lines: int = 80) -> None:
        self.args = [str(a) for a in args]
        self.name = name
        self._stderr: deque[str] = deque(maxlen=stderr_lines)
        self._stderr_cond = threading.Condition()
        self._stopping = False
        kwargs: dict = {}
        if WINDOWS:
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        try:
            self.proc = subprocess.Popen(
                self.args, stdin=subprocess.PIPE if stdin else subprocess.DEVNULL,
                stdout=subprocess.PIPE if stdout else subprocess.DEVNULL, stderr=subprocess.PIPE,
                close_fds=True, bufsize=0, env=env, **kwargs)
        except OSError as error:
            raise LiveError(f"could not start {name} ({self.args[0]}): {error}") from error
        self._stderr_thread = threading.Thread(target=self._pump_stderr, daemon=True, name=f"live-{name}-stderr")
        self._stderr_thread.start()

    @property
    def pid(self) -> int:
        return self.proc.pid

    @property
    def stopping(self) -> bool:
        return self._stopping

    def _pump_stderr(self) -> None:
        stream = self.proc.stderr
        if stream is None:
            return
        for raw in iter(stream.readline, b""):
            line = raw.decode("utf-8", "replace").rstrip()
            if not line:
                continue
            with self._stderr_cond:
                self._stderr.append(line)
                self._stderr_cond.notify_all()
            log.debug("%s: %s", self.name, line)
        with self._stderr_cond:
            self._stderr_cond.notify_all()

    def stderr_text(self, lines: int = 12) -> str:
        with self._stderr_cond:
            return "\n".join(list(self._stderr)[-lines:])

    def wait_for_stderr(self, needle: str, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        with self._stderr_cond:
            while True:
                if any(needle in line for line in self._stderr):
                    return True
                remaining = deadline - time.monotonic()
                if remaining <= 0 or (self.proc.poll() is not None and not self._stderr_thread.is_alive()):
                    return False
                self._stderr_cond.wait(min(remaining, 0.25))

    def alive(self) -> bool:
        return self.proc.poll() is None

    def returncode(self) -> int | None:
        return self.proc.poll()

    def wait(self, timeout: float | None = None) -> int | None:
        try:
            return self.proc.wait(timeout)
        except subprocess.TimeoutExpired:
            return None

    def describe_exit(self) -> str:
        code = self.proc.poll()
        detail = self.stderr_text()
        state = "is still running" if code is None else f"exited with code {code}"
        return f"{self.name} {state}" + (f": {detail}" if detail else "")

    def send_signal(self, sig: int, group: bool = True) -> None:
        if self.proc.poll() is not None:
            return
        try:
            if WINDOWS:
                if sig == signal.SIGINT:
                    self.proc.send_signal(signal.CTRL_BREAK_EVENT)
                else:
                    self.proc.terminate()
            elif group:
                os.killpg(self.proc.pid, sig)
            else:
                os.kill(self.proc.pid, sig)
        except (ProcessLookupError, PermissionError, OSError):
            pass

    def close_stdin(self) -> None:
        stream = self.proc.stdin
        if stream is not None:
            try:
                stream.close()
            except OSError:
                pass

    def stop(self, grace: float = 5.0, first: int = signal.SIGTERM, group: bool = True,
             kill: bool = True) -> int | None:
        self._stopping = True
        self.close_stdin()
        if self.proc.poll() is None:
            self.send_signal(first, group)
            code = self.wait(grace)
            if code is None and kill:
                self.send_signal(signal.SIGKILL if not WINDOWS else signal.SIGTERM, group)
                code = self.wait(5)
        for stream in (self.proc.stdout, self.proc.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
        return self.proc.poll()


class MjpegTranscoder:
    def __init__(self, input_format: str, settings: StreamSettings, *, name: str,
                 ffmpeg: str | None = None, frame_queue: int = 4) -> None:
        binary = ffmpeg or ffmpeg_binary()
        if not binary:
            raise LiveError("ffmpeg is not installed or not on PATH; the live view of devices needs it")
        edge = int(settings.max_edge)
        args = [binary, "-hide_banner", "-loglevel", "error", "-flags", "low_delay"]
        if input_format == "h264":
            args += ["-probesize", "32", "-analyzeduration", "0"]
        args += [
            "-f", input_format, "-i", "pipe:0", "-an", "-fps_mode", "passthrough",
            "-vf", f"scale='min({edge},iw)':'min({edge},ih)':force_original_aspect_ratio=decrease"
                   f":force_divisible_by=2:out_range=full,format=yuvj420p",
            "-c:v", "mjpeg", "-threads", "1", "-q:v", str(settings.ffmpeg_qscale), "-f", "mjpeg",
            "-flush_packets", "1", "pipe:1",
        ]
        self.name = name
        self.frames_out = 0
        self.bytes_in = 0
        self._queue: queue.Queue[bytes] = queue.Queue(maxsize=frame_queue)
        self._ended = threading.Event()
        self._closed = False
        self._write_lock = threading.Lock()
        self._delimit = input_format == "h264"
        self._pending_vcl = False
        self._last_write = 0.0
        self._delimit_cond = threading.Condition()
        self.process = ManagedProcess(args, name=name, stdin=True)
        self._reader = threading.Thread(target=self._read, daemon=True, name=f"live-{name}-out")
        self._reader.start()
        if self._delimit:
            threading.Thread(target=self._delimiter, daemon=True, name=f"live-{name}-aud").start()

    def _delimiter(self) -> None:
        while not self._ended.is_set():
            with self._delimit_cond:
                if not self._pending_vcl:
                    self._delimit_cond.wait(0.5)
                    continue
                wait = AUD_DELAY - (time.monotonic() - self._last_write)
                if wait > 0:
                    self._delimit_cond.wait(wait)
                    continue
                self._pending_vcl = False
            stream = self.process.proc.stdin
            with self._write_lock:
                if self._closed or stream is None:
                    return
                try:
                    stream.write(ACCESS_UNIT_DELIMITER)
                except (OSError, ValueError):
                    return

    def _note_write(self, data: bytes) -> None:
        last = data.rfind(b"\x00\x00\x01")
        if last < 0 or last + 3 >= len(data):
            return
        with self._delimit_cond:
            self._pending_vcl = 1 <= (data[last + 3] & 0x1F) <= 5
            self._last_write = time.monotonic()
            self._delimit_cond.notify_all()

    def _read(self) -> None:
        splitter = JpegSplitter()
        stream = self.process.proc.stdout
        try:
            while True:
                chunk = stream.read(READ_CHUNK) if stream is not None else b""
                if not chunk:
                    break
                for frame in splitter.feed(chunk):
                    self.frames_out += 1
                    self._offer(frame)
        except (OSError, ValueError):
            pass
        finally:
            self._ended.set()

    def _offer(self, frame: bytes) -> None:
        while True:
            try:
                self._queue.put_nowait(frame)
                return
            except queue.Full:
                try:
                    self._queue.get_nowait()
                except queue.Empty:
                    pass

    def write(self, data: bytes) -> None:
        if not data:
            return
        stream = self.process.proc.stdin
        with self._write_lock:
            if self._closed or stream is None:
                raise LiveError(f"{self.name} is closed")
            try:
                stream.write(data)
                self.bytes_in += len(data)
                if self._delimit:
                    self._note_write(data)
            except (BrokenPipeError, OSError, ValueError) as error:
                if self._closed:
                    raise LiveError(f"{self.name} is closed") from error
                self.process.wait(2)
                raise LiveError(f"ffmpeg stopped decoding the stream: {self.process.describe_exit()}") from error

    def end_input(self) -> None:
        with self._write_lock:
            self.process.close_stdin()

    @property
    def ended(self) -> bool:
        return self._ended.is_set() and self._queue.empty()

    def next_frame(self, timeout: float) -> bytes | None:
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def frames(self, stop: threading.Event, poll: float = 0.25) -> Iterator[bytes]:
        while not stop.is_set():
            frame = self.next_frame(poll)
            if frame is not None:
                yield frame
                continue
            if self.ended:
                if self._closed or stop.is_set():
                    return
                self.process.wait(2)
                raise LiveError(f"ffmpeg ended the stream: {self.process.describe_exit()}")

    def close(self) -> None:
        with self._write_lock:
            self._closed = True
        self.process.stop(grace=3.0)
        self._ended.set()


def watchdog_command(command: Sequence[str], grace: float = 20.0) -> list[str]:
    return [sys.executable, "-m", "eks_harness.live.watchdog", "--parent", str(os.getpid()),
            "--grace", str(grace), "--", *[str(c) for c in command]]
