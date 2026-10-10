from __future__ import annotations

import io
import json
import logging
import mimetypes
import re
import shutil
import struct
import subprocess
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("eks_harness.store.media")

KIND_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
FFPROBE_TIMEOUT = 30
FFMPEG_TIMEOUT = 60
THUMB_WIDTH = 480
POSTER_BOX = 1280
FFMPEG_DEMUXERS = {
    "image/png": "png_pipe",
    "image/jpeg": "jpeg_pipe",
    "image/gif": "gif",
    "image/webp": "webp_pipe",
    "video/mp4": "mov",
    "video/quicktime": "mov",
    "video/webm": "matroska",
    "audio/mpeg": "mp3",
    "audio/aac": "aac",
    "audio/wav": "wav",
    "audio/ogg": "ogg",
    "audio/flac": "flac",
    "audio/mp4": "mov",
}
WAVEFORM_COLOR = "0xd4a017"
WAVEFORM_BACKGROUND = "0x16171b"

HTMLISH_KINDS = frozenset({"dom", "mhtml", "site"})
TEXT_KINDS = frozenset({"a11y", "console", "log", "har"})
DANGEROUS_MIMES = frozenset({
    "text/html", "application/xhtml+xml", "image/svg+xml", "text/xml", "application/xml", "multipart/related",
    "message/rfc822", "application/x-mimearchive", "text/javascript", "application/javascript",
})
INLINE_TEXT_MIMES = frozenset({"text/plain", "text/csv", "text/markdown", "application/json", "application/x-ndjson",
                               "application/har+json", "text/x-log"})
KIND_MIMES = {
    "har": "application/json",
    "a11y": "text/plain",
    "log": "text/plain",
    "mhtml": "multipart/related",
    "dom": "text/html",
}
EXTENSION_MIMES = {
    ".har": "application/json",
    ".mhtml": "multipart/related",
    ".mht": "multipart/related",
    ".log": "text/plain",
    ".md": "text/markdown",
    ".ndjson": "application/x-ndjson",
    ".webp": "image/webp",
    ".webm": "video/webm",
    ".mp4": "video/mp4",
    ".m4v": "video/mp4",
    ".mov": "video/quicktime",
    ".mp3": "audio/mpeg",
    ".aac": "audio/aac",
    ".wav": "audio/wav",
    ".ogg": "audio/ogg",
    ".oga": "audio/ogg",
    ".opus": "audio/ogg",
    ".flac": "audio/flac",
    ".m4a": "audio/mp4",
    ".woff2": "font/woff2",
    ".woff": "font/woff",
    ".js": "text/javascript",
    ".mjs": "text/javascript",
    ".css": "text/css",
    ".json": "application/json",
    ".svg": "image/svg+xml",
    ".wasm": "application/wasm",
    ".avif": "image/avif",
}


@dataclass(frozen=True)
class Probe:
    mime: str
    width: int | None = None
    height: int | None = None
    duration_ms: int | None = None


def guess_mime(filename: str) -> str | None:
    suffix = Path(filename).suffix.lower()
    if suffix in EXTENSION_MIMES:
        return EXTENSION_MIMES[suffix]
    return mimetypes.guess_type(filename)[0]


def sniff_mime(head: bytes) -> str | None:
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand in (b"qt  ",):
            return "video/quicktime"
        if brand in (b"avif", b"avis"):
            return "image/avif"
        if brand in (b"heic", b"heix", b"mif1"):
            return "image/heic"
        if brand in (b"M4A ", b"M4B ", b"M4P "):
            return "audio/mp4"
        return "video/mp4"
    if head.startswith(b"\x1a\x45\xdf\xa3"):
        return "video/webm"
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return "audio/wav"
    if head.startswith(b"ID3"):
        return "audio/mpeg"
    if head.startswith(b"OggS"):
        return "audio/ogg"
    if head.startswith(b"fLaC"):
        return "audio/flac"
    if len(head) >= 2 and head[0] == 0xFF and head[1] & 0xF6 == 0xF0:
        return "audio/aac"
    if len(head) >= 2 and head[0] == 0xFF and head[1] & 0xE0 == 0xE0 and head[1] & 0x06:
        return "audio/mpeg"
    if head.startswith(b"%PDF-"):
        return "application/pdf"
    if head.startswith(b"PK\x03\x04") or head.startswith(b"PK\x05\x06"):
        return "application/zip"
    return None


def resolve_mime(declared: str | None, filename: str, head: bytes) -> str:
    sniffed = sniff_mime(head)
    if sniffed:
        return sniffed
    declared = (declared or "").split(";", 1)[0].strip().lower()
    if declared and declared != "application/octet-stream":
        return declared
    return guess_mime(filename) or "application/octet-stream"


def normalize_kind(kind: str | None) -> str | None:
    value = (kind or "").strip().lower()
    if not value:
        return None
    if value == "image":
        value = "screenshot"
    if not KIND_PATTERN.match(value):
        raise ValueError("kind must be 1-32 lowercase letters, digits, - or _, starting with a letter")
    return value


def infer_kind(mime: str, filename: str) -> str:
    suffix = Path(filename).suffix.lower()
    lowered = filename.lower()
    if suffix in (".mhtml", ".mht") or mime in ("multipart/related", "message/rfc822", "application/x-mimearchive"):
        return "mhtml"
    if suffix == ".har" or lowered.endswith(".har.json"):
        return "har"
    if mime.startswith("image/"):
        return "screenshot"
    if mime.startswith("video/"):
        return "video"
    if mime.startswith("audio/"):
        return "audio"
    if mime in ("text/html", "application/xhtml+xml"):
        return "dom"
    if "a11y" in lowered or "accessibility" in lowered:
        return "a11y"
    if "console" in lowered and (mime.startswith("text/") or mime in INLINE_TEXT_MIMES):
        return "console"
    if suffix == ".log" or mime in ("text/plain", "text/x-log"):
        return "log"
    return "file"


def mime_for_kind(kind: str, mime: str, filename: str) -> str:
    if mime in ("application/octet-stream", "") and kind in KIND_MIMES:
        return KIND_MIMES[kind]
    if kind == "har" and mime in ("text/plain", "application/octet-stream"):
        return "application/json"
    return mime


def is_inline(kind: str, mime: str) -> bool:
    if kind in HTMLISH_KINDS or mime in DANGEROUS_MIMES:
        return False
    if mime.startswith(("image/", "video/", "audio/")):
        return True
    if mime in INLINE_TEXT_MIMES:
        return True
    if kind in TEXT_KINDS and (mime.startswith("text/") or mime in INLINE_TEXT_MIMES):
        return True
    return False


def served_content_type(mime: str) -> str:
    if mime.startswith("text/") or mime in ("application/json", "application/x-ndjson", "text/javascript"):
        return f"{mime}; charset=utf-8"
    return mime


def _png_size(data: bytes) -> tuple[int, int] | None:
    if len(data) >= 24 and data[12:16] == b"IHDR":
        return struct.unpack(">II", data[16:24])
    return None


def _gif_size(data: bytes) -> tuple[int, int] | None:
    if len(data) >= 10:
        return struct.unpack("<HH", data[6:10])
    return None


def _webp_size(data: bytes) -> tuple[int, int] | None:
    chunk = data[12:16]
    if chunk == b"VP8 " and len(data) >= 30:
        width, height = struct.unpack("<HH", data[26:30])
        return width & 0x3FFF, height & 0x3FFF
    if chunk == b"VP8L" and len(data) >= 25:
        bits = int.from_bytes(data[21:25], "little")
        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    if chunk == b"VP8X" and len(data) >= 30:
        width = int.from_bytes(data[24:27], "little") + 1
        height = int.from_bytes(data[27:30], "little") + 1
        return width, height
    return None


_SOF_MARKERS = frozenset(range(0xC0, 0xD0)) - {0xC4, 0xC8, 0xCC}


def _jpeg_size(path: Path) -> tuple[int, int] | None:
    with open(path, "rb") as handle:
        return _jpeg_size_from(handle)


def jpeg_dimensions(data: bytes) -> tuple[int, int] | None:
    return _jpeg_size_from(io.BytesIO(data))


def _jpeg_size_from(handle) -> tuple[int, int] | None:
    if handle.read(2) != b"\xff\xd8":
        return None
    while True:
        byte = handle.read(1)
        while byte and byte != b"\xff":
            byte = handle.read(1)
        while byte == b"\xff":
            byte = handle.read(1)
        if not byte:
            return None
        marker = byte[0]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            continue
        if marker == 0xD9:
            return None
        length_bytes = handle.read(2)
        if len(length_bytes) < 2:
            return None
        length = struct.unpack(">H", length_bytes)[0]
        if length < 2:
            return None
        if marker in _SOF_MARKERS:
            segment = handle.read(5)
            if len(segment) < 5:
                return None
            height, width = struct.unpack(">HH", segment[1:5])
            return width, height
        handle.seek(length - 2, 1)


def image_size(path: Path, mime: str) -> tuple[int, int] | None:
    try:
        if mime == "image/jpeg":
            return _jpeg_size(path)
        with open(path, "rb") as handle:
            head = handle.read(64)
        if mime == "image/png":
            return _png_size(head)
        if mime == "image/gif":
            return _gif_size(head)
        if mime == "image/webp":
            return _webp_size(head)
    except (OSError, struct.error):
        return None
    return None


def ffprobe_binary() -> str | None:
    return shutil.which("ffprobe")


def ffmpeg_binary() -> str | None:
    return shutil.which("ffmpeg")


def sniff_file(path: Path) -> str | None:
    try:
        with open(path, "rb") as handle:
            return sniff_mime(handle.read(64))
    except OSError:
        return None


def demuxer_for(path: Path) -> str | None:
    return FFMPEG_DEMUXERS.get(sniff_file(path) or "")


def probe_video(path: Path) -> tuple[int | None, int | None, int | None]:
    demuxer = demuxer_for(path)
    if demuxer is None or not demuxer_is_video(demuxer):
        return None, None, None
    binary = ffprobe_binary()
    if binary is None:
        log.warning("ffprobe is not on PATH; video duration of %s is unknown", path.name)
        return None, None, None
    try:
        result = subprocess.run(
            [binary, "-v", "error", "-protocol_whitelist", "file", "-f", demuxer, "-select_streams", "v:0",
             "-show_entries", "stream=width,height,duration:format=duration", "-of", "json", f"file:{path}"],
            capture_output=True, text=True, timeout=FFPROBE_TIMEOUT, check=False)
    except (OSError, subprocess.TimeoutExpired) as problem:
        log.warning("ffprobe failed on %s: %s", path.name, problem)
        return None, None, None
    if result.returncode != 0:
        log.warning("ffprobe failed on %s: %s", path.name, result.stderr.strip()[:300])
        return None, None, None
    try:
        data = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        return None, None, None
    stream = (data.get("streams") or [{}])[0]
    duration = stream.get("duration") or (data.get("format") or {}).get("duration")
    try:
        duration_ms = int(round(float(duration) * 1000)) if duration not in (None, "N/A") else None
    except (TypeError, ValueError):
        duration_ms = None
    width = stream.get("width")
    height = stream.get("height")
    return (int(width) if width else None), (int(height) if height else None), duration_ms


def demuxer_is_video(demuxer: str) -> bool:
    return demuxer in ("mov", "matroska")


def probe_audio(path: Path) -> int | None:
    demuxer = demuxer_for(path)
    if demuxer is None:
        return None
    binary = ffprobe_binary()
    if binary is None:
        log.warning("ffprobe is not on PATH; audio duration of %s is unknown", path.name)
        return None
    try:
        result = subprocess.run(
            [binary, "-v", "error", "-protocol_whitelist", "file", "-f", demuxer, "-select_streams", "a:0",
             "-show_entries", "stream=duration:format=duration", "-of", "json", f"file:{path}"],
            capture_output=True, text=True, timeout=FFPROBE_TIMEOUT, check=False)
    except (OSError, subprocess.TimeoutExpired) as problem:
        log.warning("ffprobe failed on %s: %s", path.name, problem)
        return None
    if result.returncode != 0:
        log.warning("ffprobe failed on %s: %s", path.name, result.stderr.strip()[:300])
        return None
    try:
        data = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        return None
    stream = (data.get("streams") or [{}])[0]
    duration = stream.get("duration") or (data.get("format") or {}).get("duration")
    try:
        return int(round(float(duration) * 1000)) if duration not in (None, "N/A") else None
    except (TypeError, ValueError):
        return None


def probe(path: Path, mime: str) -> Probe:
    sniffed = sniff_file(path)
    if sniffed != mime:
        return Probe(mime)
    if mime.startswith("image/"):
        size = image_size(path, mime)
        if size:
            return Probe(mime, size[0], size[1])
        return Probe(mime)
    if mime.startswith("video/"):
        width, height, duration_ms = probe_video(path)
        return Probe(mime, width, height, duration_ms)
    if mime.startswith("audio/"):
        return Probe(mime, duration_ms=probe_audio(path))
    return Probe(mime)


def make_thumbnail(source: Path, dest: Path, mime: str, width: int = THUMB_WIDTH) -> bool:
    if mime.startswith("audio/"):
        return _waveform(source, dest, mime, width, width * 9 // 16)
    return _extract_frame(source, dest, mime, f"scale='max(2,trunc(min({width},iw)/2)*2)':-2:flags=bicubic")


def make_poster(source: Path, dest: Path, mime: str, box: int = POSTER_BOX) -> bool:
    if mime.startswith("audio/"):
        return _waveform(source, dest, mime, box, box * 9 // 16)
    return _extract_frame(source, dest, mime,
                          f"scale='min({box},iw)':'min({box},ih)':force_original_aspect_ratio=decrease:flags=bicubic,"
                          "scale='max(2,trunc(iw/2)*2)':'max(2,trunc(ih/2)*2)'")


def poster_size(width: int | None, height: int | None, box: int = POSTER_BOX) -> tuple[int, int] | None:
    if not width or not height:
        return None
    scale = min(1.0, box / width, box / height)
    return max(2, int(width * scale) // 2 * 2), max(2, int(height * scale) // 2 * 2)


def _waveform(source: Path, dest: Path, mime: str, width: int, height: int) -> bool:
    sniffed = sniff_file(source)
    demuxer = FFMPEG_DEMUXERS.get(sniffed or "")
    if demuxer is None or sniffed != mime:
        return False
    binary = ffmpeg_binary()
    if binary is None:
        log.warning("ffmpeg is not on PATH; no waveform for %s", source.name)
        return False
    width -= width % 2
    height -= height % 2
    wave_height = height * 3 // 5 // 2 * 2
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".tmp.jpg")
    graph = (f"[0:a:0]aformat=channel_layouts=mono,"
             f"showwavespic=s={width}x{wave_height}:split_channels=0:colors={WAVEFORM_COLOR}:scale=sqrt:draw=full[wave];"
             f"color=c={WAVEFORM_BACKGROUND}:s={width}x{height}[bg];"
             f"[bg][wave]overlay=0:(H-h)/2:format=auto,format=yuvj420p[out]")
    command = [binary, "-nostdin", "-v", "error", "-y", "-protocol_whitelist", "file",
               "-f", demuxer, "-i", f"file:{source}", "-filter_complex", graph, "-map", "[out]",
               "-frames:v", "1", "-q:v", "4", "-f", "image2", "-c:v", "mjpeg", str(tmp)]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=FFMPEG_TIMEOUT, check=False)
    except (OSError, subprocess.TimeoutExpired) as problem:
        log.warning("waveform of %s failed: %s", source.name, problem)
        tmp.unlink(missing_ok=True)
        return False
    if result.returncode != 0 or not tmp.is_file() or tmp.stat().st_size == 0:
        log.warning("waveform of %s failed: %s", source.name, result.stderr.strip()[:300])
        tmp.unlink(missing_ok=True)
        return False
    tmp.replace(dest)
    return True


def _extract_frame(source: Path, dest: Path, mime: str, scale: str) -> bool:
    sniffed = sniff_file(source)
    demuxer = FFMPEG_DEMUXERS.get(sniffed or "")
    if demuxer is None or sniffed != mime:
        return False
    binary = ffmpeg_binary()
    if binary is None:
        log.warning("ffmpeg is not on PATH; no thumbnail for %s", source.name)
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".tmp.jpg")
    command = [binary, "-nostdin", "-v", "error", "-y", "-protocol_whitelist", "file"]
    if demuxer_is_video(demuxer):
        command += ["-ss", "0"]
    command += ["-f", demuxer, "-i", f"file:{source}", "-frames:v", "1", "-vf",
                f"{scale},format=yuvj420p", "-q:v", "5", "-f", "image2",
                "-c:v", "mjpeg", str(tmp)]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=FFMPEG_TIMEOUT, check=False)
    except (OSError, subprocess.TimeoutExpired) as problem:
        log.warning("thumbnail of %s failed: %s", source.name, problem)
        tmp.unlink(missing_ok=True)
        return False
    if result.returncode != 0 or not tmp.is_file() or tmp.stat().st_size == 0:
        log.warning("thumbnail of %s failed: %s", source.name, result.stderr.strip()[:300])
        tmp.unlink(missing_ok=True)
        return False
    tmp.replace(dest)
    return True


COUNT_LIMIT_BYTES = 256 * 1024 * 1024
LINE_COUNT_KINDS = frozenset({"log", "console", "a11y"})


def content_counts(path: Path, kind: str, mime: str, size: int) -> dict:
    if size > COUNT_LIMIT_BYTES:
        return {}
    if kind == "har":
        requests = har_entry_count(path)
        return {"requests": requests} if requests is not None else {}
    if kind in LINE_COUNT_KINDS or mime.startswith("text/") or mime in INLINE_TEXT_MIMES:
        if kind in HTMLISH_KINDS:
            return {}
        return {"lines": line_count(path, size)}
    return {}


def har_entry_count(path: Path) -> int | None:
    try:
        with open(path, "rb") as handle:
            data = json.load(handle)
    except (OSError, ValueError, UnicodeDecodeError, RecursionError):
        return None
    entries = (data.get("log") or {}).get("entries") if isinstance(data, dict) else None
    return len(entries) if isinstance(entries, list) else None


def line_count(path: Path, size: int) -> int:
    if size == 0:
        return 0
    count = 0
    last = b""
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            count += chunk.count(b"\n")
            last = chunk[-1:]
    return count if last == b"\n" else count + 1
