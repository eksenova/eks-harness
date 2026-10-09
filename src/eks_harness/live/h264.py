from __future__ import annotations

import struct
from dataclasses import dataclass, field
from pathlib import Path

START_CODE = b"\x00\x00\x00\x01"
NAL_IDR = 5
NAL_SPS = 7
NAL_PPS = 8
MAX_NAL_BYTES = 64 * 1024 * 1024
CONTAINER_BOXES = ("moov", "trak", "mdia", "minf", "stbl")
STREAMABLE_BOXES = ("moov", "moof")
VISUAL_SAMPLE_ENTRY_BYTES = 78


class StreamSyncError(ValueError):
    pass


@dataclass(frozen=True)
class AvcConfig:
    nal_length_size: int
    sps: tuple[bytes, ...] = field(default_factory=tuple)
    pps: tuple[bytes, ...] = field(default_factory=tuple)

    def parameter_sets(self) -> bytes:
        return b"".join(START_CODE + nal for nal in (*self.sps, *self.pps))

    def to_bytes(self) -> bytes:
        out = bytearray([1, self.sps[0][1] if self.sps else 0x64, self.sps[0][2] if self.sps else 0,
                         self.sps[0][3] if self.sps else 0x32, 0xFC | (self.nal_length_size - 1),
                         0xE0 | len(self.sps)])
        for nal in self.sps:
            out += struct.pack(">H", len(nal)) + nal
        out.append(len(self.pps))
        for nal in self.pps:
            out += struct.pack(">H", len(nal)) + nal
        return bytes(out)


def parse_avcc(payload: bytes) -> AvcConfig:
    if len(payload) < 7 or payload[0] != 1:
        raise ValueError("not an AVCDecoderConfigurationRecord")
    length_size = (payload[4] & 0x03) + 1
    pos = 5
    sps_count = payload[pos] & 0x1F
    pos += 1
    sps = []
    for _ in range(sps_count):
        (size,) = struct.unpack(">H", payload[pos:pos + 2])
        pos += 2
        sps.append(bytes(payload[pos:pos + size]))
        pos += size
    pps_count = payload[pos]
    pos += 1
    pps = []
    for _ in range(pps_count):
        (size,) = struct.unpack(">H", payload[pos:pos + 2])
        pos += 2
        pps.append(bytes(payload[pos:pos + size]))
        pos += size
    if not sps or not pps:
        raise ValueError("the avcC record carries no SPS or PPS")
    return AvcConfig(length_size, tuple(sps), tuple(pps))


def _iter_boxes(data: bytes, start: int = 0, end: int | None = None):
    end = len(data) if end is None else end
    pos = start
    while pos + 8 <= end:
        size, kind = struct.unpack(">I4s", data[pos:pos + 8])
        header = 8
        if size == 1:
            if pos + 16 > end:
                return
            (size,) = struct.unpack(">Q", data[pos + 8:pos + 16])
            header = 16
        elif size == 0:
            size = end - pos
        if size < header or pos + size > end:
            return
        yield kind.decode("latin-1"), pos + header, pos + size
        pos += size


def _find_avcc_in(data: bytes, start: int, end: int, path: tuple[str, ...]) -> bytes | None:
    for kind, body, box_end in _iter_boxes(data, start, end):
        if kind != path[0]:
            continue
        if len(path) == 1:
            return bytes(data[body:box_end])
        inner = body
        if kind == "stsd":
            inner = body + 8
        elif kind in ("avc1", "avc3"):
            inner = body + VISUAL_SAMPLE_ENTRY_BYTES
        found = _find_avcc_in(data, inner, box_end, path[1:])
        if found is not None:
            return found
    return None


def find_avcc(data: bytes) -> AvcConfig | None:
    for entry in ("avc1", "avc3"):
        payload = _find_avcc_in(data, 0, len(data), (*CONTAINER_BOXES, "stsd", entry, "avcC"))
        if payload is not None:
            return parse_avcc(payload)
    return None


def avcc_from_file(path: Path) -> AvcConfig | None:
    return find_avcc(path.read_bytes())


class AnnexBCutter:
    def __init__(self) -> None:
        self._buf = bytearray()
        self._synced = False

    def feed(self, data: bytes) -> bytes:
        self._buf += data
        if not self._synced:
            first = self._buf.find(b"\x00\x00\x01")
            if first < 0:
                keep = self._buf[-2:]
                self._buf = bytearray(keep)
                return b""
            if first > 0 and self._buf[first - 1] == 0:
                first -= 1
            del self._buf[:first]
            self._synced = True
        last = self._buf.rfind(b"\x00\x00\x01")
        if last <= 0:
            return b""
        if self._buf[last - 1] == 0:
            last -= 1
        if last <= 0:
            return b""
        out = bytes(self._buf[:last])
        del self._buf[:last]
        return out

    def flush(self) -> bytes:
        out = bytes(self._buf) if self._synced else b""
        self.reset()
        return out

    def reset(self) -> None:
        self._buf = bytearray()
        self._synced = False


class MovStreamDemuxer:
    def __init__(self, config: AvcConfig | None = None) -> None:
        self.config = config
        self.mode = "boxes"
        self._buf = bytearray()
        self._pending = bytearray()
        self.samples = 0
        self.idr_frames = 0

    def feed(self, data: bytes) -> bytes:
        if self.mode == "passthrough":
            return bytes(data)
        self._buf += data
        self._pending += data
        out = bytearray()
        while True:
            if self.mode == "boxes":
                if not self._parse_box():
                    break
                if self.mode == "passthrough":
                    out += self._pending
                    self._pending = bytearray()
                    self._buf = bytearray()
                    return bytes(out)
            elif self.mode == "samples":
                chunk = self._parse_sample()
                if chunk is None:
                    break
                out += chunk
        if self.mode == "samples":
            self._pending = bytearray()
        return bytes(out)

    def _parse_box(self) -> bool:
        if len(self._buf) < 8:
            return False
        size, kind = struct.unpack(">I4s", self._buf[:8])
        header = 8
        if size == 1:
            if len(self._buf) < 16:
                return False
            (size,) = struct.unpack(">Q", self._buf[8:16])
            header = 16
        name = kind.decode("latin-1")
        if not all(32 <= b < 127 for b in kind):
            raise StreamSyncError(f"unexpected bytes where a QuickTime box header should be: {bytes(kind)!r}")
        if name in STREAMABLE_BOXES:
            self.mode = "passthrough"
            return True
        if name == "mdat":
            if self.config is None:
                raise StreamSyncError("the recording has no codec configuration before its samples")
            del self._buf[:header]
            self.mode = "samples"
            return True
        if size == 0:
            raise StreamSyncError(f"open-ended {name} box before the samples")
        if size < header:
            raise StreamSyncError(f"invalid size {size} for the {name} box")
        if len(self._buf) < size:
            return False
        del self._buf[:size]
        return True

    def _parse_sample(self) -> bytes | None:
        config = self.config
        width = config.nal_length_size
        if len(self._buf) < width:
            return None
        size = int.from_bytes(self._buf[:width], "big")
        if size <= 0 or size > MAX_NAL_BYTES:
            raise StreamSyncError(f"invalid NAL unit length {size} in the recording")
        if len(self._buf) < width + size:
            return None
        nal = bytes(self._buf[width:width + size])
        del self._buf[:width + size]
        if nal[0] & 0x80:
            raise StreamSyncError("NAL unit with the forbidden bit set; the stream is out of sync")
        self.samples += 1
        kind = nal[0] & 0x1F
        if kind in (NAL_SPS, NAL_PPS):
            return START_CODE + nal
        if kind == NAL_IDR:
            self.idr_frames += 1
            return config.parameter_sets() + START_CODE + nal
        return START_CODE + nal

