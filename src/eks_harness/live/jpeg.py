from __future__ import annotations

SOI = b"\xff\xd8"
MAX_FRAME_BYTES = 32 * 1024 * 1024
_STANDALONE = {0x01, *range(0xD0, 0xD8)}


class JpegSplitter:
    def __init__(self, max_frame_bytes: int = MAX_FRAME_BYTES) -> None:
        self.max_frame_bytes = max_frame_bytes
        self._buf = bytearray()
        self._pos = 0
        self._in_scan = False
        self._started = False
        self.dropped_bytes = 0

    def feed(self, data: bytes) -> list[bytes]:
        if data:
            self._buf += data
        frames: list[bytes] = []
        while True:
            frame = self._next()
            if frame is None:
                break
            frames.append(frame)
        if len(self._buf) > self.max_frame_bytes:
            self.dropped_bytes += len(self._buf)
            self._reset(keep=b"")
        return frames

    def _reset(self, keep: bytes) -> None:
        self._buf = bytearray(keep)
        self._pos = 0
        self._in_scan = False
        self._started = False

    def _resync(self) -> None:
        start = self._buf.find(SOI, 1)
        if start < 0:
            self.dropped_bytes += len(self._buf)
            tail = self._buf[-1:] if self._buf[-1:] == b"\xff" else b""
            self._reset(bytes(tail))
        else:
            self.dropped_bytes += start
            self._reset(bytes(self._buf[start:]))

    def _next(self) -> bytes | None:
        buf = self._buf
        if not self._started:
            start = buf.find(SOI)
            if start < 0:
                self.dropped_bytes += max(0, len(buf) - 1)
                tail = buf[-1:] if buf[-1:] == b"\xff" else b""
                self._reset(bytes(tail))
                return None
            if start:
                self.dropped_bytes += start
                del buf[:start]
            self._started = True
            self._pos = 2
            self._in_scan = False
        while True:
            if self._in_scan:
                j = buf.find(b"\xff", self._pos)
                if j < 0 or j + 1 >= len(buf):
                    self._pos = len(buf) - 1 if j >= 0 else len(buf)
                    return None
                nxt = buf[j + 1]
                if nxt == 0x00 or 0xD0 <= nxt <= 0xD7:
                    self._pos = j + 2
                    continue
                if nxt == 0xFF:
                    self._pos = j + 1
                    continue
                self._in_scan = False
                self._pos = j
                continue
            i = self._pos
            if i + 2 > len(buf):
                return None
            if buf[i] != 0xFF:
                self._resync()
                return None
            if buf[i + 1] == 0xFF:
                self._pos = i + 1
                continue
            marker = buf[i + 1]
            if marker == 0xD9:
                end = i + 2
                frame = bytes(buf[:end])
                del buf[:end]
                self._started = False
                self._pos = 0
                self._in_scan = False
                return frame
            if marker == 0xD8:
                self.dropped_bytes += i
                del buf[:i]
                self._pos = 2
                continue
            if marker in _STANDALONE:
                self._pos = i + 2
                continue
            if i + 4 > len(buf):
                return None
            length = (buf[i + 2] << 8) | buf[i + 3]
            if length < 2:
                self._resync()
                return None
            if i + 2 + length > len(buf):
                return None
            self._pos = i + 2 + length
            if marker == 0xDA:
                self._in_scan = True


def is_jpeg(data: bytes) -> bool:
    return len(data) > 4 and data[:2] == SOI and data[-2:] == b"\xff\xd9"
